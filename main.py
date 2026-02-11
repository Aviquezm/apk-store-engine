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
    candidatos_fb = []
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            for nombre in z.namelist():
                if not (nombre.lower().endswith(('.png', '.webp')) and 'res/' in nombre): continue
                if 'notification' in nombre.lower(): continue
                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    if w < 48 or abs(w-h)>2: continue
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
# LIMPIEZA
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        pkg_nuevo = str(pkg_nuevo_raw).strip().lower()
        
        for i, r in enumerate(registros):
            pkg_viejo = str(r.get('Pkg')).strip().lower()
            id_viejo = str(r.get('ID Drive', '')).strip()
            
            if pkg_viejo == pkg_nuevo and id_viejo != id_archivo_nuevo:
                try: drive_service.files().delete(fileId=id_viejo).execute()
                except: pass
                try: dbx.files_delete_v2(f"/{r.get('Nombre', '').replace(' ', '_')}_v{r.get('Version', '')}.apk")
                except: pass
                filas_a_borrar.append(i + 2)
                archivos_borrados += 1

        if filas_a_borrar:
            for fila_num in sorted(filas_a_borrar, reverse=True):
                sheet.delete_row(fila_num)
                time.sleep(1.5)
    except: pass

# ---------------------------------------------------------
# GENERADOR WEB (FIX VERSION OBTAINIUM)
# ---------------------------------------------------------
def generar_archivos_finales(sheet):
    print("🔄 Generando Web 'index.html' corregida...")
    registros = sheet.get_all_records()
    
    html = """<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Tienda APK</title>
    <style>
        body { background: #121212; color: #fff; font-family: sans-serif; padding: 20px; }
        .app { background: #1e1e1e; padding: 15px; margin-bottom: 15px; border-radius: 12px; display: flex; align-items: center; border: 1px solid #333; }
        .icon { width: 60px; height: 60px; border-radius: 15px; margin-right: 15px; object-fit: cover; }
        .btn { background: #00E676; color: #000; padding: 10px 20px; text-decoration: none; border-radius: 25px; font-weight: bold; display: inline-block; margin-top: 5px;}
    </style></head><body><h1>📦 Mis Apps</h1>"""

    for r in registros:
        if not r.get('Pkg'): continue
        nombre = r.get('Nombre', 'App')
        version = r.get('Version', '1.0')
        link = r.get('Link APK')
        icon = r.get('Link Icono')
        
        # AQUÍ ESTÁ EL ARREGLO MÁGICO:
        # Ponemos "v{version}" DENTRO del texto del enlace.
        # Obtainium leerá: "Descargar Spotify v8.9" y sabrá que es la versión 8.9.
        html += f"""
        <div class="app">
            <img src="{icon}" class="icon">
            <div>
                <h3>{nombre}</h3>
                <a href="{link}" class="btn">Descargar {nombre} v{version}</a>
            </div>
        </div>"""
    
    html += "</body></html>"
    with open("index.html", "w", encoding='utf-8') as f: f.write(html)
    
    # Backup JSON
    repo_data = {"apps": registros}
    with open("index.json", "w", encoding='utf-8') as f: json.dump(repo_data, f)

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor V26 (Fix Obtainium)...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    # PROCESAMIENTO
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

    if nuevos:
        notificar(f"👷‍♂️ <b>Procesando {len(nuevos)} APKs</b>")
        for item in nuevos:
            try:
                file_id = item['id']
                temp_apk = "temp.apk"
                
                # Descargar
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # Analizar
                apk = APK(temp_apk)
                nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                apk_hash = calcular_hash(temp_apk)
                apk_size = os.path.getsize(temp_apk)
                
                # Icono
                icon_data = extraer_icono_precision(temp_apk, apk.application)
                link_icon = "https://via.placeholder.com/150"
                if icon_data:
                    with open("temp_icon.png", "wb") as f: f.write(icon_data)
                    url_icon = subir_a_dropbox(dbx, "temp_icon.png", f"icon_{apk.package}.png")
                    if url_icon: link_icon = url_icon
                    os.remove("temp_icon.png")

                # Subir APK
                dest_path = f"/{nombre_limpio}_{apk.version_name}.apk"
                with open(temp_apk, "rb") as f: dbx.files_upload(f.read(), dest_path, mode=WriteMode('overwrite'))
                try: link_apk = dbx.sharing_create_shared_link_with_settings(dest_path).url.replace("?dl=0", "?dl=1")
                except: link_apk = dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links[0].url.replace("?dl=0", "?dl=1")

                # Guardar
                sheet.append_row([
                    nombre_limpio, "Publicado", link_apk, apk.version_name, 
                    apk.package, link_icon, file_id, "Dropbox", 
                    str(apk.version_code), apk_hash, str(apk_size)
                ])
                eliminar_rastros_anteriores(sheet, drive_service, dbx, apk.package, file_id)
                notificar(f"✅ <b>{nombre_limpio}</b> v{apk.version_name} listo.")

            except Exception as e:
                print(f"Error {item['name']}: {e}")
            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)

    # REGENERAR WEB SIEMPRE
    generar_archivos_finales(sheet)
    print("✅ Web actualizada para Obtainium.")

if __name__ == "__main__":
    main()
