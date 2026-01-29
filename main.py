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
    """Busca agresivamente la mejor imagen de icono dentro del APK"""
    try:
        # 1. Obtener toda la info de badging
        cmd = ['aapt', 'dump', 'badging', file_path]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
        
        # 2. Extraer el NOMBRE BASE del icono (ej: ic_launcher)
        # Buscamos la línea: application-icon-640:'res/mipmap-anydpi-v26/ic_launcher.xml'
        icon_match = re.search(r"icon='([^']+)'", out)
        if not icon_match:
            icon_match = re.search(r"application-icon-\d+:'([^']+)'", out)
        
        nombre_buscar = "ic_launcher" # Default por si acaso
        if icon_match:
            # Sacamos solo el nombre del archivo sin extensión (ej: ic_launcher)
            nombre_buscar = os.path.basename(icon_match.group(1)).split('.')[0]

        with zipfile.ZipFile(file_path, 'r') as z:
            nombres_en_zip = z.namelist()
            
            # 3. LISTA DE CANDIDATOS: Buscamos imágenes que contengan ese nombre
            # Filtramos por .png o .webp y que NO estén en carpetas "v26" (que suelen ser XML)
            candidatos = [
                n for n in nombres_en_zip 
                if nombre_buscar in n 
                and n.lower().endswith(('.png', '.webp'))
                and 'v26' not in n
            ]

            # 4. Si no hay candidatos con el nombre oficial, buscamos "icon" o "launcher" en general
            if not candidatos:
                candidatos = [
                    n for n in nombres_en_zip 
                    if ('icon' in n.lower() or 'launcher' in n.lower()) 
                    and n.lower().endswith(('.png', '.webp'))
                    and 'v26' not in n
                ]

            if candidatos:
                # ORDENAR POR CALIDAD: El archivo más grande (bytes) suele ser el de mejor resolución
                candidatos.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                return candidatos[0] # Retornamos el mejor
                
    except Exception as e:
        print(f"Error en cacería: {e}")
    return None

async def main():
    print("🚀 Iniciando Extractor Atómico v7...")
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

            await client.send_message(ADMIN_ID, f"🕵️ **Analizando:** `{file_name}`")
            temp_apk, final_icon = "temp.apk", "icon_final.png"
            DEFAULT_ICON = "default_icon.png"
            
            try:
                # Descarga
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # Datos con AAPT
                cmd = ['aapt', 'dump', 'badging', temp_apk]
                out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
                pkg = re.search(r"package: name='([^']+)'", out).group(1)
                ver = re.search(r"versionCode='([^']+)'", out).group(1)
                label_m = re.search(r"application-label:'([^']+)'", out)
                label = label_m.group(1) if label_m else pkg

                # --- EXTRACCIÓN AGRESIVA ---
                ruta_interna = cazar_icono_real(temp_apk)
                usa_default = True

                if ruta_interna:
                    try:
                        with zipfile.ZipFile(temp_apk, 'r') as z:
                            with z.open(ruta_interna) as src, open(final_icon, "wb") as trg:
                                trg.write(src.read())
                        usa_default = False
                        await client.send_message(ADMIN_ID, f"🎯 **Icono REAL hallado:** `{ruta_interna}`")
                    except: pass

                if usa_default:
                    if os.path.exists(DEFAULT_ICON):
                        shutil.copyfile(DEFAULT_ICON, final_icon)
                        await client.send_message(ADMIN_ID, f"⚠️ No se halló PNG. Usando default para `{label}`.")

                # Subidas
                icon_msg_id = ""
                if os.path.exists(final_icon):
                    msg_f = await client.send_file(CHANNEL_ID, final_icon, caption=f"🖼 Icono: {label}")
                    icon_msg_id = str(msg_f.id)

                msg_apk = await client.send_file(CHANNEL_ID, temp_apk, caption=f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}", thumb=final_icon if os.path.exists(final_icon) else None)

                # Excel
                sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
                await client.send_message(ADMIN_ID, f"✨ `{label}` listo.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"🔥 Error: {e}")
            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)
                if os.path.exists(final_icon): os.remove(final_icon)

if __name__ == "__main__":
    asyncio.run(main())
