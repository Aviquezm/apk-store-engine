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

# --- CONFIGURACIÓN AUTOMÁTICA ---
# IDs FIJOS (Ya configurados con tus datos)
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

# Configuración desde GitHub Secrets
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

async def main():
    print("🌍 Conectando a Google y Telegram...")
    
    # 1. Conexión Google
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    # 2. Conexión Telegram
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    # 3. Leer Inventario Actual (Excel)
    registros = sheet.get_all_records()
    # Creamos una lista de los IDs de Drive que YA hemos procesado para no repetirlos
    archivos_procesados = [str(r['ID_Archivo_Drive']) for r in registros if r['ID_Archivo_Drive'] != '']

    # 4. Escanear Carpeta de Drive (El Sabueso)
    print(f"📂 Escaneando carpeta {DRIVE_FOLDER_ID}...")
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    if not items:
        print("📭 La carpeta está vacía.")
    
    async with client:
        for item in items:
            file_id = item['id']
            file_name = item['name']

            # Si no es un APK, lo ignoramos
            if not file_name.lower().endswith('.apk'):
                continue

            # Si ya está en el Excel, lo ignoramos (ya fue procesado)
            if file_id in archivos_procesados:
                print(f"⏭️ {file_name} ya existe en el sistema. Saltando.")
                continue

            # --- ¡ARCHIVO NUEVO DETECTADO! ---
            print(f"🆕 Procesando nuevo archivo: {file_name}")
            
            # Avisar en el canal que estamos trabajando (Mensaje temporal)
            status_msg = await client.send_message(CHANNEL_ID, f"⚙️ **Procesando:** `{file_name}`\n⏳ Por favor espere...")

            try:
                # 1. Descargar
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                fh.seek(0)
                
                with open("temp.apk", "wb") as f:
                    f.write(fh.read())

                # 2. Analizar APK
                apk_info = APK("temp.apk")
                package_name = apk_info.get_package()
                version_code = int(apk_info.get_androidversion_code())
                app_label = apk_info.get_app_name()

                # 3. Verificar si es una ACTUALIZACIÓN (Borrar viejos)
                # Buscamos en el Excel si ya existía este PackageName
                cell = sheet.find(package_name)
                if cell:
                    fila_vieja = cell.row
                    datos_viejos = sheet.row_values(fila_vieja)
                    # Asumimos estructura: [Nombre, Estado, Info, Version, Package, MsgID, DriveID]
                    # Ajusta índices según tu Excel real, aquí busco por columnas lógicas
                    msg_id_viejo = datos_viejos[5] # Columna F
                    drive_id_viejo = datos_viejos[6] # Columna G
                    
                    print(f"♻️ Actualización detectada. Eliminando versión anterior...")
                    try:
                        await client.delete_messages(CHANNEL_ID, [int(msg_id_viejo)])
                        drive_service.files().delete(fileId=drive_id_viejo).execute()
                    except:
                        pass # Si falla borrar, seguimos igual
                    
                    # Borramos la fila vieja del Excel para poner la nueva limpia al final
                    sheet.delete_rows(fila_vieja)

                # 4. Subir a Telegram (Archivo Final)
                caption = f"✅ **{app_label}**\n📦 `{package_name}`\n🔢 Versión: {version_code}\n📅 Actualizado automáticamente"
                msg_final = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, parse_mode='md')

                # 5. Registrar en Excel (Nueva Fila)
                # Columnas: Nombre | Estado | Notas | Version | Package | MsgID | DriveID
                nueva_fila = [app_label, "Publicado", "Auto-Upload", version_code, package_name, msg_final.id, file_id]
                sheet.append_row(nueva_fila)

                print(f"✅ Éxito: {file_name} publicado.")
                
                # Borrar el mensaje de "Procesando"
                await client.delete_messages(CHANNEL_ID, [status_msg.id])

            except Exception as e:
                print(f"❌ Error procesando {file_name}: {e}")
                await client.edit_message(CHANNEL_ID, status_msg.id, f"⚠️ Error procesando `{file_name}`.")
            
            finally:
                if os.path.exists("temp.apk"):
                    os.remove("temp.apk")

if __name__ == "__main__":
    asyncio.run(main())
