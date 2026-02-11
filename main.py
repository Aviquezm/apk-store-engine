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

# --- UTILIDADES ---
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        requests.post(url, data=data)
    except Exception as e: print(f"⚠️ Telegram Error: {e}")

def limpiar_texto(texto):
    if not texto: return ""
    return str(texto).strip().lower().replace('\n', '').replace('\r', '').replace('\t', '')

def calcular_hash(file_path):
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

# --- EXTRACCIÓN DE ICONOS V22 (Invencible) ---
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    candidatos_fb = [] 
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            for nombre in z.namelist():
                nombre_lc = nombre.lower()
                if not (nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre): continue
                if 'notification' in nombre_lc or 'abc_' in nombre_lc: continue 

                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    if abs(w - h) > 2 or w < 48: continue 
                    
                    puntuacion = 0
                    if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 10000
                    if 'ic_launcher' in nombre_lc: puntuacion += 4500
                    
                    prioridad_fb = 0
                    if 96 <= w <= 192: prioridad_fb = 3 
                    elif 193 <= w <= 512: prioridad_fb = 2
                    elif w > 512: prioridad_fb = 1
                    candidatos_fb.append((nombre, prioridad_fb, w, data))

                    if puntuacion > 0 and puntuacion > mejor_puntuacion:
                        mejor_puntuacion = puntuacion
                        mejor_data = data
                except: continue
            
            if mejor_puntuacion > 1000: return mejor_data
            if candidatos_fb:
                candidatos_fb.sort(key=lambda x: (x[1], x[2]), reverse=True)
                return candidatos_fb[0][3]
            return None
    except: return None

# --- LIMPIEZA ---
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        pkg_nuevo = limpiar_texto(pkg_nuevo_raw)
        
        for i, r in enumerate(registros):
            pkg_viejo = limpiar_texto(r.get('Pkg'))
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
        return archivos_borrados
    except: return 0

# --- GENERADOR DE HTML (El Puente para Obtainium) ---
def generar_html_obtainium(sheet):
    print("🔄 Generando 'index.html' para Obtainium...")
    registros = sheet.get_all_records()
    
    html_content = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Repositorio APKs</title>
        <style>
            body { font-family: sans-serif; background: #121212; color: white; padding: 20px; }
            .app { border: 1px solid #333; padding: 15px; margin-bottom: 10px; border-radius: 8px; display: flex; align-items: center; }
            .icon { width: 50px; height: 50px; border-radius: 10px; margin-right: 15px; }
            a { color: #00e676; text-decoration: none; font-weight: bold; }
        </style>
    </head>
    <body>
        <h1>Catálogo de Actualizaciones</h1>
    """

    for r in registros:
        if not r.get('Pkg'): continue
        nombre = r.get('Nombre')
        version = r.get('Version')
        link_apk = r.get('Link APK')
        link_icono = r.get('Link Icono', 'https://via.placeholder.com/50')
        pkg = r.get('Pkg')

        # Creamos un bloque HTML por cada app. 
        # Obtainium buscará el enlace que termina en .apk o el link de Dropbox
        html_content += f"""
        <div class="app" id="{pkg}">
            <img src="{link_icono}" class="icon">
            <div>
                <h3>{nombre}</h3>
                <p>Version: {version}</p>
                <p>ID: {pkg}</p>
                <a href="{link_apk}">Descargar APK</a>
            </div>
        </div>
        """

    html_content += "</body></html>"
    
    with open("index.html", "w") as f: f.write(html_content)

# --- MAIN ---
def conectar_dropbox():
    return dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)

def subir_a_dropbox(dbx, file_path, dest_filename):
    dest_path = f"/{dest_filename}"
    with open(file_path, "rb") as f:
        dbx.files_upload(f.read(), dest_path, mode=WriteMode('overwrite'))
    try: return dbx.sharing_create_shared_link_with_settings(dest_path).url.replace("?dl=0", "?dl=1")
    except: return dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links[0].url.replace("?dl=0", "?dl=1")

def main():
    print("🚀 Iniciando Motor V24 (Modo Híbrido)...")
    
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    
    nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

    if nuevos:
        notificar(f"👷‍♂️ <b>Procesando {len(nuevos)} APKs</b>")
        for item in nuevos:
            file_id = str(item['id']).strip()
            temp_apk = "temp.apk"
            try:
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                apk = APK(temp_apk)
                nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                icon_data = extraer_icono_precision(temp_apk, apk.application)
                apk_hash = calcular_hash(temp_apk)
                apk_size = os.path.getsize(temp_apk)
                
                link_apk = subir_a_dropbox(dbx, temp_apk, f"{nombre_limpio}_{apk.version_name}.apk")
                
                link_icon = "https://via.placeholder.com/150" 
                if icon_data:
                    with open("temp_icon.png", "wb") as f: f.write(icon_data)
                    url = subir_a_dropbox(dbx, "temp_icon.png", f"icon_{apk.package}.png")
                    if url: link_icon = url
                    os.remove("temp_icon.png")

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
    
    # SIEMPRE REGENERAR HTML Y JSON AL FINAL
    generar_html_obtainium(sheet)

if __name__ == "__main__":
    main()
