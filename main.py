import os
import json
import gspread
import io
import asyncio
import subprocess
import re
import zipfile
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from androguard.core.bytecodes.apk import APK

# --- TUS DATOS FIJOS ---
ADMIN_ID = 761087529  # TU ID para reportes privados
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

# Configuración de Entorno
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# --- FUNCIÓN BLINDADA (LEE APKs DIFICILES) ---
def get_apk_data_robust(file_path):
    package, version_code, label, icon_path = None, None, None, None
    
    # Intento 1: Androguard
    try:
        apk = APK(file_path)
        package = apk.get_package()
        version_code = int(apk.get_androidversion_code())
        label = apk.get_app_name()
        icon_path = apk.get_icon_path()
    except: pass

    # Intento 2: AAPT (Fuerza Bruta)
    if not package or not version_code:
        print("🔧 Activando AAPT...")
        try:
            cmd = ['aapt', 'dump', 'badging', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            output = result.stdout
            
            if not package:
                package = re.search(r"package: name='([^']+)'", output).group(1)
            if not version_code:
                version_code = int(re.search(r"versionCode='([^']+)'", output).group(1))
            if not label:
                l_match = re.search(r"application-label:'([^']+)'", output)
                label = l_match.group(1) if l_match else package
            if not icon_path:
                # Busca el icono más grande
                icons = re.findall(r"application-icon-\d+:'([^']+)'", output)
                icon_path = icons[-1] if icons else re.search(r"icon='([^']+)'", output).group(1)
        except Exception as e:
            print(f"❌ Error grave leyendo APK: {e}")
            raise Exception("No se pudo leer el APK.")

    return package, version_code, label, icon_path

async def main():
    print("🌍 Iniciando sistema v3 (Iconos + Auto)...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    procesados = [str(r['ID_Drive']) for r in registros if str(r['ID_Drive']).strip() != '']

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        if not items: return

        for item in items:
            file_id = item['id']
            file_name = item['name']

            if not file_name.lower().endswith('.apk') or file_id in procesados:
                continue

            # --- NUEVO APK DETECTADO ---
            status_msg = await client.send_message(ADMIN_ID, f"⚙️ **Procesando:** `{file_name}`")
            extracted_icon = "temp_icon.png"
            
            try:
                # 1. Descargar
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open("temp.apk", "wb") as f: f.write(fh.read())

                # 2. Analizar
                pkg, ver, name, icon_path = get_apk_data_robust("temp.apk")
                
                # 3. Extraer Icono
                icon_msg_id = ""
                if icon_path:
                    try:
                        with zipfile.ZipFile("temp.apk", 'r') as z:
                            with z.open(icon_path) as s, open(extracted_icon, "wb") as t:
                                t.write(s.read())
                        msg_icon = await client.send_file(CHANNEL_ID, extracted_icon, caption=f"Icono: {name}")
                        icon_msg_id = msg_icon.id
                    except: pass

                # 4. Limpieza (Borrar versión vieja)
                cell = sheet.find(pkg)
                if cell:
                    fila = cell.row
                    try:
                        old_data = sheet.row_values(fila)
                        # Borrar mensajes viejos (APK e Icono)
                        await client.delete_messages(CHANNEL_ID, [int(old_data[5])]) # Col F
                        if len(old_data) > 7 and old_data[7]: 
                            await client.delete_messages(CHANNEL_ID, [int(old_data[7])]) # Col H
                        drive_service.files().delete(fileId=old_data[6]).execute() # Col G
                        sheet.delete_rows(fila)
                        await client.send_message(ADMIN_ID, "🗑️ Versión vieja eliminada.")
                    except: pass

                # 5. Subir APK
                caption = f"✅ **{name}**\n📦 `{pkg}`\n🔢 v{ver}"
                thumb = extracted_icon if os.path.exists(extracted_icon) else None
                msg_apk = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, thumb=thumb)

                # 6. Guardar en Excel
                # Orden: Nombre|Estado|Notas|VersionCode|PackageName|MsgID|ID_Drive|IconoURL
                new_row = [name, "Publicado", "Auto", ver, pkg, msg_apk.id, file_id, icon_msg_id]
                sheet.append_row(new_row)

                await client.edit_message(ADMIN_ID, status_msg.id, f"✅ **Listo:** `{name}` publicado.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"❌ Error: {str(e)}")
            
            finally:
                if os.path.exists("temp.apk"): os.remove("temp.apk")
                if os.path.exists(extracted_icon): os.remove(extracted_icon)

if __name__ == "__main__":
    asyncio.run(main())
