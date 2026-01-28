import os
import json
import gspread
import io
import asyncio
import subprocess
import re
import zipfile
import shutil
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

def cazar_icono_real(file_path):
    """Busca una imagen real dentro del APK"""
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
        icon_paths = re.findall(r"icon='([^']+)'|application-icon-\d+:'([^']+)'", out)
        rutas_sugeridas = [path for group in icon_paths for path in group if path]
        
        with zipfile.ZipFile(file_path, 'r') as z:
            nombres_en_zip = z.namelist()
            # Estrategia: Buscar PNG/WebP oficial o por nombre base
            for r in reversed(rutas_sugeridas):
                if r.lower().endswith(('.png', '.webp')) and r in nombres_en_zip:
                    return r
                nombre_base = os.path.basename(r).split('.')[0]
                candidatos = [n for n in nombres_en_zip if nombre_base in n and n.lower().endswith(('.png', '.webp'))]
                if candidatos:
                    candidatos.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                    return candidatos[0]
    except: pass
    return None

async def main():
    print("🚀 Iniciando Motor con Icono de Seguridad...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    procesados = [str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')]

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    async with client:
        for item in items:
            file_id, file_name = item['id'], item['name']
            if not file_name.lower().endswith('.apk') or file_id in procesados: continue

            await client.send_message(ADMIN_ID, f"🕵️ **Procesando:** `{file_name}`")
            temp_apk, final_icon = "temp.apk", "icon_a_subir.png"
            DEFAULT_ICON = "default_icon.png"
            
            try:
                # 1. Descarga
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # 2. Info básica
                cmd = ['aapt', 'dump', 'badging', temp_apk]
                out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
                pkg = re.search(r"package: name='([^']+)'", out).group(1)
                ver = re.search(r"versionCode='([^']+)'", out).group(1)
                label = re.search(r"application-label:'([^']+)'", out)
                label = label.group(1) if label else pkg

                # 3. Lógica del Icono (Real o Default)
                ruta_interna = cazar_icono_real(temp_apk)
                usa_default = True

                if ruta_interna:
                    try:
                        with zipfile.ZipFile(temp_apk, 'r') as z:
                            with z.open(ruta_interna) as src, open(final_icon, "wb") as trg:
                                trg.write(src.read())
                        usa_default = False
                        await client.send_message(ADMIN_ID, f"✅ Icono real extraído.")
                    except: pass

                if usa_default:
                    if os.path.exists(DEFAULT_ICON):
                        shutil.copyfile(DEFAULT_ICON, final_icon)
                        await client.send_message(ADMIN_ID, f"⚠️ Usando icono predeterminado.")
                    else:
                        await client.send_message(ADMIN_ID, f"❌ No se encontró `{DEFAULT_ICON}` en GitHub.")

                # 4. Subida a Telegram
                icon_msg_id = ""
                if os.path.exists(final_icon):
                    msg_f = await client.send_file(CHANNEL_ID, final_icon, caption=f"🖼 Icono: {label}")
                    icon_msg_id = str(msg_f.id)

                msg_apk = await client.send_file(CHANNEL_ID, temp_apk, caption=f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}", thumb=final_icon if os.path.exists(final_icon) else None)

                # 5. Guardar en Excel (A-H)
                sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
                await client.send_message(ADMIN_ID, f"✨ `{label}` publicado con éxito.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"🔥 Error con `{file_name}`: {e}")
            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)
                if os.path.exists(final_icon): os.remove(final_icon)

if __name__ == "__main__":
    asyncio.run(main())
