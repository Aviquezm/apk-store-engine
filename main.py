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

# --- DATOS FIJOS ---
ADMIN_ID = 761087529 
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def extraer_todo_con_aapt(file_path):
    """Extrae absolutamente toda la info usando la herramienta oficial de Android"""
    pkg, ver, label, icon_path = None, None, None, None
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        out = result.stdout
        
        # 1. Datos técnicos
        pkg = re.search(r"package: name='([^']+)'", out).group(1)
        ver = int(re.search(r"versionCode='([^']+)'", out).group(1))
        
        # 2. Nombre de la App
        label_match = re.search(r"application-label:'([^']+)'", out)
        if not label_match: label_match = re.search(r"application-label-es:'([^']+)'", out)
        label = label_match.group(1) if label_match else pkg

        # 3. Ruta del Icono (Buscamos la mejor resolución)
        icons = re.findall(r"application-icon-\d+:'([^']+)'", out)
        if not icons:
            icon_std = re.search(r"icon='([^']+)'", out)
            if icon_std: icons.append(icon_std.group(1))
        
        # Filtramos para evitar XMLs (iconos adaptativos) que Telegram no procesa
        icon_path = next((i for i in reversed(icons) if i.lower().endswith(('.png', '.webp', '.jpg'))), None)
        
    except Exception as e:
        print(f"Error crítico AAPT: {e}")
    return pkg, ver, label, icon_path

async def main():
    print("🌍 Iniciando Extractor Blindado v5...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    # Leemos IDs ya procesados para no repetir
    registros = sheet.get_all_records()
    procesados = [str(r['ID_Drive']) for r in registros if 'ID_Drive' in r and str(r['ID_Drive']).strip()]

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        for item in items:
            file_id, file_name = item['id'], item['name']
            if not file_name.lower().endswith('.apk') or file_id in procesados: continue

            await client.send_message(ADMIN_ID, f"🕵️ **Procesando:** `{file_name}`")
            
            # Descarga
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open("temp.apk", "wb") as f: f.write(fh.read())

            # Análisis total con AAPT
            pkg, ver, label, icon_internal_path = extraer_todo_con_aapt("temp.apk")
            
            if not pkg:
                await client.send_message(ADMIN_ID, f"❌ Imposible leer `{file_name}`. Archivo corrupto.")
                continue

            icon_msg_id = ""
            temp_icon = "extracted_icon.png"

            # Extracción física del icono desde el ZIP
            if icon_internal_path:
                try:
                    with zipfile.ZipFile("temp.apk", 'r') as z:
                        with z.open(icon_internal_path) as source, open(temp_icon, "wb") as target:
                            target.write(source.read())
                    
                    # Subir foto del icono al canal
                    msg_foto = await client.send_file(CHANNEL_ID, temp_icon, caption=f"🖼 Icono de {label}")
                    icon_msg_id = str(msg_foto.id)
                except Exception as e:
                    await client.send_message(ADMIN_ID, f"⚠️ No pude sacar el icono de `{label}`: {e}")

            # Subir el APK al canal
            caption = f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}"
            msg_apk = await client.send_file(
                CHANNEL_ID, 
                "temp.apk", 
                caption=caption, 
                thumb=temp_icon if os.path.exists(temp_icon) else None
            )

            # --- GUARDAR EN GOOGLE SHEETS ---
            # Asegúrate de que el orden sea: Nombre|Estado|Notas|Ver|Pkg|MsgID|DriveID|IconoURL
            sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
            
            # Limpieza
            if os.path.exists("temp.apk"): os.remove("temp.apk")
            if os.path.exists(temp_icon): os.remove(temp_icon)
            await client.send_message(ADMIN_ID, f"✨ `{label}` publicado con éxito.")

if __name__ == "__main__":
    asyncio.run(main())
