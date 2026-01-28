import os
import json
import gspread
import io
import asyncio
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from androguard.core.bytecodes.apk import APK

# Configuración desde GitHub Secrets
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

async def main():
    # --- 1. CONEXIÓN A GOOGLE ---
    print("🌍 Conectando a Google...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    
    # Usamos el ID directo que ya sabemos que funciona
    sheet = client_gs.open_by_key("1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo").sheet1
    filas = sheet.get_all_records()

    # --- 2. PREPARAR EL BOT DE TELEGRAM ---
    print("🤖 Iniciando Bot de Telegram...")
    client = TelegramClient('bot_session', API_ID, API_HASH)
    
    # Encendemos el bot explícitamente antes de usarlo
    await client.start(bot_token=BOT_TOKEN)

    # --- 3. PROCESAR ARCHIVOS ---
    async with client:
        for i, fila in enumerate(filas, start=2):
            if fila['Estado'] == 'Pendiente':
                file_id = fila['ID_Archivo_Drive']
                app_name = fila['Nombre']
                
                print(f"📥 Descargando {app_name} de Drive...")
                
                # Descarga segura
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                
                fh.seek(0)
                with open("temp.apk", "wb") as f:
                    f.write(fh.read())
                
                # Análisis del APK
                apk_info = APK("temp.apk")
                package_name = apk_info.get_package()
                version_code = int(apk_info.get_androidversion_code())

                # --- LIMPIEZA AUTOMÁTICA ---
                for j, fila_antigua in enumerate(filas, start=2):
                    if fila_antigua['PackageName'] == package_name and fila_antigua['Estado'] == 'Publicado':
                        print(f"🧹 Limpiando versión vieja de {package_name}...")
                        try:
                            # Borrar mensaje anterior
                            await client.delete_messages(CHANNEL_ID, [int(fila_antigua['Mensaje_ID'])])
                        except Exception as e:
                            print(f"Nota: No se pudo borrar mensaje antiguo ({e})")
                        
                        try:
                            # Borrar archivo de Drive
                            drive_service.files().delete(fileId=fila_antigua['ID_Archivo_Drive']).execute()
                        except Exception as e:
                            print(f"Nota: No se pudo borrar archivo de Drive ({e})")
                        
                        sheet.update_cell(j, 3, 'Eliminado (Auto-Clean)')

                # --- SUBIDA AL CANAL ---
                print(f"📤 Subiendo a Telegram...")
                caption = f"✅ **{app_name}**\n📦 `{package_name}`\n🔢 Versión: {version_code}"
                
                # El bot sube el archivo
                msg = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, parse_mode='md')

                # --- ACTUALIZAR HOJA ---
                sheet.update_cell(i, 3, 'Publicado')
                sheet.update_cell(i, 4, version_code)
                sheet.update_cell(i, 5, package_name)
                sheet.update_cell(i, 6, msg.id)
                
                os.remove("temp.apk")
                print(f"🚀 ¡Éxito! {app_name} publicado.")

if __name__ == "__main__":
    asyncio.run(main())
