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

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# ---------------------------------------------------------
# 🔍 CAZA ICONO REAL (FUERZA BRUTA - SIN ANDROGUARD)
# ---------------------------------------------------------
def cazar_icono_real(apk_path):
    try:
        # Usamos AAPT (Herramienta oficial) en lugar de librerías Python
        cmd = ['aapt', 'dump', 'badging', apk_path]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
        
        # Buscar rutas de iconos reportadas por Android
        icon_entries = re.findall(r"application-icon-\d+:'([^']+)'", out)
        default_icon = re.search(r"icon='([^']+)'", out)
        if default_icon: icon_entries.append(default_icon.group(1))

        prioridades = ['xxxhdpi', 'xxhdpi', 'xhdpi', 'hdpi', 'mdpi']
        candidatos = []

        with zipfile.ZipFile(apk_path, 'r') as z:
            nombres = z.namelist()

            # 1. Buscar coincidencia exacta
            for icon_path in icon_entries:
                if icon_path in nombres and icon_path.lower().endswith(('.png', '.webp', '.jpg')):
                    return icon_path 

            # 2. Buscar por nombre base (ej: ic_launcher)
            nombres_base = set()
            for icon_path in icon_entries:
                base = os.path.splitext(os.path.basename(icon_path))[0]
                nombres_base.add(base)
            
            if not nombres_base:
                nombres_base.update(['ic_launcher', 'icon', 'app_icon', 'logo'])

            for n in nombres:
                if not n.lower().endswith(('.png', '.webp', '.jpg')): continue
                if 'build-data' in n or 'META-INF' in n: continue

                # Búsqueda laxa: Si contiene el nombre, sirve
                nombre_archivo = os.path.basename(n).split('.')[0]
                if any(base in nombre_archivo for base in nombres_base):
                    candidatos.append(n)

            if not candidatos: return None

            # 3. Elegir la imagen más pesada/grande
            def score(path):
                try:
                    return z.getinfo(path).file_size
                except: return 0

            candidatos.sort(key=score, reverse=True)
            return candidatos[0]

    except Exception as e:
        print(f"⚠️ Error leve cazando icono: {e}")
    return None

def obtener_info_aapt(apk_path):
    """Extrae datos usando SOLO aapt para evitar errores de Androguard"""
    try:
        cmd = ['aapt', 'dump', 'badging', apk_path]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        out = res.stdout
        
        # Package
        pkg_match = re.search(r"package: name='([^']+)'", out)
        pkg = pkg_match.group(1) if pkg_match else "com.unknown.app"
        
        # Versión
        ver_match = re.search(r"versionCode='([^']+)'", out)
        ver = ver_match.group(1) if ver_match else "1"
        
        # Nombre (Label)
        label_match = re.search(r"application-label:'([^']+)'", out)
        label = label_match.group(1) if label_match else pkg
        
        return pkg, ver, label
    except Exception:
        return None, None, None

# ---------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------
async def main():
    print("🚀 Iniciando Motor Blindado (Sin Androguard)...")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    procesados = {str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')}

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    async with client:
        for item in items:
            file_id = item['id']
            file_name = item['name']

            if not file_name.lower().endswith('.apk'): continue
            if file_id in procesados: continue

            await client.send_message(ADMIN_ID, f"🛡️ **Procesando:** `{file_name}`")
            temp_apk = "temp.apk"
            final_icon = "icon_final.png"
            DEFAULT_ICON = "default_icon.png"

            try:
                # 1. DESCARGA
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # 2. INFO CON AAPT (INDISPENSABLE PARA NO FALLAR)
                pkg, ver, label = obtener_info_aapt(temp_apk)
                
                if not pkg or pkg == "com.unknown.app":
                    # Si falla AAPT, es que el archivo está muy corrupto
                    await client.send_message(ADMIN_ID, f"❌ Archivo corrupto o ilegible: `{file_name}`. Saltando.")
                    # Lo marcamos como procesado (con error) para que no se encicle
                    sheet.append_row([file_name, "Error", "Corrupto", "", "", "", file_id, ""])
                    continue

                # 3. EXTRAER ICONO
                ruta_icono = cazar_icono_real(temp_apk)
                usa_default = True

                if ruta_icono:
                    try:
                        with zipfile.ZipFile(temp_apk, 'r') as z:
                            with z.open(ruta_icono) as src, open(final_icon, "wb") as trg:
                                trg.write(src.read())
                        usa_default = False
                        await client.send_message(ADMIN_ID, f"🎯 Icono: `{ruta_icono}`")
                    except: pass

                if usa_default and os.path.exists(DEFAULT_ICON):
                    shutil.copyfile(DEFAULT_ICON, final_icon)

                # 4. SUBIDAS
                icon_msg_id = ""
                if os.path.exists(final_icon):
                    msg_icon = await client.send_file(CHANNEL_ID, final_icon, caption=f"🖼 Icono: {label}")
                    icon_msg_id = str(msg_icon.id)

                msg_apk = await client.send_file(
                    CHANNEL_ID, temp_apk, 
                    caption=f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}",
                    thumb=final_icon if os.path.exists(final_icon) else None
                )

                # 5. GUARDAR
                sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
                await client.send_message(ADMIN_ID, f"✨ `{label}` publicado.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"🔥 Error grave en `{file_name}`: {e}")
            
            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)
                if os.path.exists(final_icon): os.remove(final_icon)

if __name__ == "__main__":
    asyncio.run(main())
