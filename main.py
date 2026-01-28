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

# --- CONFIGURACIÓN DE SEGURIDAD (SECRETS) ---
ADMIN_ID = int(os.environ['ADMIN_ID'])
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']

API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def buscar_icono_real(file_path):
    """Busca una imagen PNG/WebP incluso si el APK dice que el icono es XML"""
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
        
        # 1. Intentar obtener la ruta oficial
        icons = re.findall(r"application-icon-\d+:'([^']+)'", out)
        if not icons:
            std = re.search(r"icon='([^']+)'", out)
            if std: icons.append(std.group(1))
        
        # Filtrar solo imágenes (no XML)
        for path in reversed(icons):
            if path.lower().endswith(('.png', '.webp', '.jpg')):
                return path
        
        # 2. SI TODO FALLA: Buscar por nombre en el ZIP
        with zipfile.ZipFile(file_path, 'r') as z:
            nombres = z.namelist()
            # Buscamos cualquier cosa que se llame ic_launcher o icon y sea imagen
            posibles = [n for n in nombres if ('ic_launcher' in n or 'icon' in n) and n.lower().endswith(('.png', '.webp'))]
            if posibles:
                # Ordenar por tamaño (el que más pesa suele ser el de mejor calidad)
                posibles.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                return posibles[0]
                
    except: pass
    return None

async def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    procesados = [str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')]

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        for item in items:
            file_id, file_name = item['id'], item['name']
            if not file_name.lower().endswith('.apk') or file_id in procesados: continue

            await client.send_message(ADMIN_ID, f"🕵️ **Procesando:** `{file_name}`")
            temp_apk, temp_icon = "temp.apk", "icon.png"
            
            try:
                # Descarga
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # Info con AAPT
                cmd = ['aapt', 'dump', 'badging', temp_apk]
                out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
                pkg = re.search(r"package: name='([^']+)'", out).group(1)
                ver = re.search(r"versionCode='([^']+)'", out).group(1)
                label = re.search(r"application-label:'([^']+)'", out)
                label = label.group(1) if label else pkg

                # Icono
                icon_path = buscar_icono_real(temp_apk)
                icon_msg_id = ""
                if icon_path:
                    with zipfile.ZipFile(temp_apk, 'r') as z:
                        with z.open(icon_path) as source, open(temp_icon, "wb") as target:
                            target.write(source.read())
                    msg_foto = await client.send_file(CHANNEL_ID, temp_icon, caption=f"🖼 Icono: {label}")
                    icon_msg_id = str(msg_foto.id)

                # Subir APK
                msg_apk = await client.send_file(CHANNEL_ID, temp_apk, caption=f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}", thumb=temp_icon if os.path.exists(temp_icon) else None)

                # Guardar (Asegúrate que el orden A-H sea correcto en tu Excel)
                sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
                await client.send_message(ADMIN_ID, f"✨ `{label}` listo. Icono ID: {icon_msg_id}")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"❌ Error en `{file_name}`: {e}")
            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)
                if os.path.exists(temp_icon): os.remove(temp_icon)

if __name__ == "__main__":
    asyncio.run(main())
