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

# --- DATOS FIJOS (Costa Rica GMT-6) ---
ADMIN_ID = 761087529 
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_best_icon_path(file_path):
    """Usa AAPT para listar todos los iconos y devuelve el de mayor resolución"""
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        output = result.stdout
        # Capturamos iconos de todas las densidades
        icons = re.findall(r"application-icon-\d+:'([^']+)'", output)
        default_icon = re.search(r"icon='([^']+)'", output)
        if default_icon: icons.append(default_icon.group(1))
        # Filtramos solo imágenes reales (no XML)
        valid = [i for i in icons if i.lower().endswith(('.png', '.webp', '.jpg'))]
        return valid[-1] if valid else (os.path.basename(default_icon.group(1)).replace('.xml','') if default_icon else None)
    except: return None

async def main():
    print("🌍 Iniciando Extractor Nivel Dios...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    procesados = [str(r['ID_Drive']) for r in registros if 'ID_Drive' in r and str(r['ID_Drive']).strip()]

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        for item in items:
            file_id, file_name = item['id'], item['name']
            if not file_name.lower().endswith('.apk') or file_id in procesados: continue

            await client.send_message(ADMIN_ID, f"🔎 **Analizando:** `{file_name}`")
            
            # Descarga
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open("temp.apk", "wb") as f: f.write(fh.read())

            apk = APK("temp.apk")
            pkg, ver, label = apk.get_package(), int(apk.get_androidversion_code()), apk.get_app_name()
            icon_msg_id, ext_icon = "", "icon_final.png"

            # --- EXTRACCIÓN AGRESIVA ---
            hint = get_best_icon_path("temp.apk")
            try:
                with zipfile.ZipFile("temp.apk", 'r') as z:
                    all_files = z.namelist()
                    target = None
                    if hint in all_files and hint.lower().endswith(('.png', '.webp')):
                        target = hint
                    else:
                        search = hint if hint else "ic_launcher"
                        matches = [f for f in all_files if search in f and f.lower().endswith(('.png', '.webp'))]
                        if matches:
                            matches.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                            target = matches[0]

                    if target:
                        with z.open(target) as s, open(ext_icon, "wb") as t: t.write(s.read())
                        m_icon = await client.send_file(CHANNEL_ID, ext_icon, caption=f"🖼 Icono de {label}")
                        icon_msg_id = str(m_icon.id)
                        await client.send_message(ADMIN_ID, f"✅ Icono extraído de: `{target}`")
                    else:
                        await client.send_message(ADMIN_ID, "❌ No se encontró imagen PNG/WebP para el icono.")
            except Exception as e: await client.send_message(ADMIN_ID, f"⚠️ Error icono: {e}")

            # Subida APK
            caption = f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}"
            msg_apk = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, thumb=ext_icon if os.path.exists(ext_icon) else None)

            # Guardar en Excel (A-H)
            sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
            
            if os.path.exists("temp.apk"): os.remove("temp.apk")
            if os.path.exists(ext_icon): os.remove(ext_icon)
            await client.send_message(ADMIN_ID, f"✨ `{label}` completada con éxito.")

if __name__ == "__main__":
    asyncio.run(main())
