import os
import json
import io
import time
import hashlib
import dropbox
import gspread
import zipfile
import re
import requests
from datetime import datetime
from PIL import Image
from dropbox.files import WriteMode
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pyaxmlparser import APK 

# --- CONFIGURACIÓN ---
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']
REPO_URL = os.environ['REPO_URL']

DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') 
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      data={"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"})
    except: pass

def calcular_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(4096), b""): sha256.update(block)
    return sha256.hexdigest()

def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            for nombre in z.namelist():
                if not (nombre.lower().endswith(('.png', '.webp')) and 'res/' in nombre): continue
                if 'notification' in nombre.lower(): continue
                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    if w < 48: continue
                    puntuacion = 0
                    if 'ic_launcher' in nombre: puntuacion += 500
                    if w > 100: puntuacion += 100
                    if puntuacion > mejor_puntuacion:
                        mejor_puntuacion = puntuacion
                        mejor_data = data
                except: continue
            return mejor_data
    except: return None

# ---------------------------------------------------------
# GENERADOR WEB V29 (Optimizado para Obtainium)
# ---------------------------------------------------------
def generar_archivos_finales(sheet):
    print("🔄 Generando Web V29...")
    registros = sheet.get_all_records()
    
    html = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tienda APK</title>
    <style>
        body { background: #121212; color: #fff; font-family: sans-serif; padding: 20px; }
        .app { background: #1e1e1e; padding: 15px; margin-bottom: 15px; border-radius: 12px; display: flex; align-items: center; border: 1px solid #333; }
        .icon { width: 64px; height: 64px; border-radius: 15px; margin-right: 15px; object-fit: cover; background: #333; }
        .btn { background: #00E676; color: #000; padding: 10px 20px; text-decoration: none; border-radius: 25px; font-weight: bold; display: inline-block; margin-top:5px; }
    </style></head><body><h1 style="text-align:center;color:#00E676;">📦 Mis Apps</h1>"""

    for r in registros:
        if not r.get('Pkg'): continue
        nombre = r.get('Nombre', 'App')
        version = r.get('Version', '1.0')
        link = r.get('Link APK')
        
        # FIX ICONOS: Usamos el dominio directo de contenidos de Dropbox
        raw_icon = str(r.get('Link Icono', ''))
        icon = raw_icon.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("?dl=0", "").replace("?dl=1", "")
        
        # FIX OBTAINIUM: El texto del enlace es CLAVE: "NombreApp vVersion"
        html += f"""
        <div class="app">
            <img src="{icon}" class="icon" onerror="this.src='https://via.placeholder.com/64'">
            <div>
                <h3>{nombre}</h3>
                <a href="{link}" class="btn">{nombre} v{version}</a>
            </div>
        </div>"""
    
    html += "</body></html>"
    with open("index.html", "w", encoding='utf-8') as f: f.write(html)
    
    # Generamos JSONs para compatibilidad
    with open("index.json", "w") as f: json.dump({"apps": registros}, f)

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor V29...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    # Procesar APKs
    try:
        registros = sheet.get_all_records()
        procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

        if nuevos:
            notificar(f"👷‍♂️ <b>Procesando {len(nuevos)} APKs</b>")
            for item in nuevos:
                temp_apk = "temp.apk"
                try:
                    request = drive_service.files().get_media(fileId=item['id'])
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    fh.seek(0)
                    with open(temp_apk, "wb") as f: f.write(fh.read())

                    apk = APK(temp_apk)
                    nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                    
                    # Icono
                    icon_data = extraer_icono_precision(temp_apk, apk.application)
                    link_icon = "https://via.placeholder.com/64"
                    if icon_data:
                        with open("temp.png", "wb") as f: f.write(icon_data)
                        with open("temp.png", "rb") as f: dbx.files_upload(f.read(), f"/icon_{apk.package}.png", mode=WriteMode('overwrite'))
                        l = dbx.sharing_create_shared_link_with_settings(f"/icon_{apk.package}.png").url
                        link_icon = l.replace("?dl=0", "?dl=1")
                        os.remove("temp.png")

                    # APK
                    path = f"/{nombre}_{apk.version_name}.apk"
                    with open(temp_apk, "rb") as f: dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("?dl=0", "?dl=1")

                    sheet.append_row([nombre, "Publicado", link_apk, apk.version_name, apk.package, link_icon, item['id'], "Dropbox", str(apk.version_code), calcular_hash(temp_apk), str(os.path.getsize(temp_apk))])
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                except Exception as e: print(e)
                finally: 
                    if os.path.exists(temp_apk): os.remove(temp_apk)
    except Exception as e: print(e)

    generar_archivos_finales(sheet)
    print("✅ Web V29 Generada.")

if __name__ == "__main__":
    main()
