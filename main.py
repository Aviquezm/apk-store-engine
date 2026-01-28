import os
import json
import gspread
import io
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from androguard.core.bytecodes.apk import APK

# 1. Cargar configuración desde los Secrets de GitHub
# Estos datos son los que ya metiste en la "caja fuerte" de GitHub
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

# Permisos para entrar a Google Sheets y Google Drive
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

async def main():
    # --- CONEXIÓN A GOOGLE ---
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    
    # Abrimos la hoja por su nombre exacto
    sheet = client_gs.open_by_key("1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo").sheet1
    filas = sheet.get_all_records()

    # --- CONEXIÓN A TELEGRAM (MTProto con Identidad de Bot) ---
    # Esto es lo que permite saltar el límite de 50MB
    async with TelegramClient('bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN) as client:
        
        for i, fila in enumerate(filas, start=2):
            if fila['Estado'] == 'Pendiente':
                file_id = fila['ID_Archivo_Drive']
                app_name = fila['Nombre']
                
                print(f"📥 Descargando {app_name} de Drive...")
                
                # Descargamos el archivo a la memoria (para que Google no analice virus)
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                
                fh.seek(0)
                temp_filename = "temp.apk"
                with open(temp_filename, "wb") as f:
                    f.write(fh.read())
                
                # --- EXTRACCIÓN DE DATOS (Para Neo Store y Limpieza) ---
                apk_info = APK(temp_filename)
                package_name = apk_info.get_package()
                version_code = int(apk_info.get_androidversion_code())

                # --- LÓGICA DE LIMPIEZA AUTOMÁTICA ---
                # Buscamos si esta app ya existía para borrar el rastro viejo
                for j, fila_antigua in enumerate(filas, start=2):
                    if fila_antigua['PackageName'] == package_name and fila_antigua['Estado'] == 'Publicado':
                        print(f"🧹 Limpiando rastro de la versión antigua: {package_name}")
                        
                        # A. Borrar mensaje del canal de Telegram
                        try:
                            await client.delete_messages(CHANNEL_ID, [int(fila_antigua['Mensaje_ID'])])
                        except Exception as e:
                            print(f"No se pudo borrar el mensaje de Telegram: {e}")
                        
                        # B. Borrar el archivo viejo de Google Drive (Limpieza de tus 200GB)
                        try:
                            drive_service.files().delete(fileId=fila_antigua['ID_Archivo_Drive']).execute()
                        except Exception as e:
                            print(f"No se pudo borrar el archivo de Drive: {e}")
                        
                        # C. Marcar en la hoja que ya fue eliminado
                        sheet.update_cell(j, 3, 'Eliminado (Auto-Clean)')

                # --- SUBIDA AL CANAL ---
                print(f"📤 Subiendo a TheApkStoreChannel...")
                caption = f"✅ **{app_name}**\n📦 `{package_name}`\n🔢 Versión: {version_code}"
                
                # Subida de archivo real (no un link)
                msg = await client.send_file(CHANNEL_ID, temp_filename, caption=caption, parse_mode='md')

                # --- ACTUALIZAR REGISTRO ---
                # Guardamos los nuevos datos para la próxima limpieza
                sheet.update_cell(i, 3, 'Publicado')
                sheet.update_cell(i, 4, version_code)
                sheet.update_cell(i, 5, package_name)
                sheet.update_cell(i, 6, msg.id)
                
                # Borramos el archivo temporal de GitHub
                os.remove(temp_filename)
                print(f"🚀 ¡Todo listo! {app_name} publicado y entorno limpio.")

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
