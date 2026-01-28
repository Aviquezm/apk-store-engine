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

# --- DATOS ---
ADMIN_ID = 761087529
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_apk_info_completa(file_path):
    """Extrae info y busca el icono más real posible"""
    pkg, ver, label, icon_name = None, None, None, None
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        out = res.stdout
        
        pkg = re.search(r"package: name='([^']+)'", out).group(1)
        ver = int(re.search(r"versionCode='([^']+)'", out).group(1))
        
        label_match = re.search(r"application-label:'([^']+)'", out)
        label = label_match.group(1) if label_match else pkg
        
        # Buscamos el nombre base del icono (ej: ic_launcher)
        icon_match = re.search(r"icon='([^']+)'", out)
        if icon_match:
            icon_name = os.path.basename(icon_match.group(1))
    except: pass
    return pkg, ver, label, icon_name

async def main():
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

            await client.send_message(ADMIN_ID, f"🚀 **Procesando:** `{file_name}`")
            
            # Descarga
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open("temp.apk", "wb") as f: f.write(fh.read())

            pkg, ver, label, icon_name = get_apk_info_completa("temp.apk")
            icon_msg_id = ""

            # --- BUSQUEDA AGRESIVA DE ICONO ---
            if icon_name:
                icon_name_clean = icon_name.replace('.xml', '') # Ignorar XML
                try:
                    with zipfile.ZipFile("temp.apk", 'r') as z:
                        # Buscamos todos los archivos que contengan el nombre del icono y sean .png
                        candidatos = [n for n in z.namelist() if icon_name_clean in n and n.lower().endswith('.png')]
                        
                        if candidatos:
                            # Ordenamos por tamaño para llevar el de mejor calidad (el que más pesa)
                            candidatos.sort(key=lambda n: z.getinfo(n).file_size, reverse=True)
                            mejor_icono = candidatos[0]
                            
                            with z.open(mejor_icono) as s, open("icon.png", "wb") as t:
                                t.write(s.read())
                            
                            m_icon = await client.send_file(CHANNEL_ID, "icon.png", caption=f"🖼 Icono de {label}")
                            icon_msg_id = str(m_icon.id)
                except: pass

            # Subir APK
            caption = f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}"
            msg_apk = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, thumb="icon.png" if os.path.exists("icon.png") else None)

            # --- GUARDAR EN EXCEL ---
            # Nombre(A) | Estado(B) | Notas(C) | Ver(D) | Pkg(E) | MsgID(F) | DriveID(G) | IconoURL(H)
            new_row = [label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id]
            sheet.append_row(new_row)
            
            if os.path.exists("temp.apk"): os.remove("temp.apk")
            if os.path.exists("icon.png"): os.remove("icon.png")
            await client.send_message(ADMIN_ID, f"✅ `{label}` terminado. Icon ID: {icon_msg_id}")

if __name__ == "__main__":
    asyncio.run(main())
