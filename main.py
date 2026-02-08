import os
import json
import io
import shutil
import dropbox
import gspread
import zipfile
import re
from datetime import datetime
from PIL import Image
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pyaxmlparser import APK 

# --- CONFIGURACIÓN ---
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']
REPO_URL = os.environ.get('REPO_URL', 'https://aviquezm.github.io/apk-store-engine/') 

DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# 1. MOTOR DE EXTRACCIÓN GEOMÉTRICO (Filtro 1:1)
# ---------------------------------------------------------
def extraer_icono_arquitecto(apk_path, app_name):
    """
    Busca el icono real asegurándose de que sea CUADRADO.
    """
    mejor_puntuacion = -1
    mejor_data = None
    
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            app_clean = app_name.lower().replace(" ", "")
            
            for nombre in archivos:
                if nombre.lower().endswith(('.png', '.webp')) and 'res/' in nombre:
                    # Ignorar iconos de notificación (suelen ser muy pequeños y blancos)
                    if 'notification' in nombre.lower() or 'abc_' in nombre.lower(): continue
                    
                    try:
                        data = z.read(nombre)
                        img = Image.open(io.BytesIO(data))
                        ancho, alto = img.size
                        
                        # FILTRO CRÍTICO: Debe ser cuadrado (o casi)
                        if abs(ancho - alto) > 2: continue # Si no es 1:1, fuera.
                        
                        # FILTRO DE TAMAÑO: Entre 144 y 512px
                        if not (140 <= ancho <= 700): continue
                        
                        puntuacion = 0
                        nombre_lc = nombre.lower()
                        
                        # Puntos por palabras clave
                        if 'launcher' in nombre_lc: puntuacion += 1000
                        if app_clean in nombre_lc: puntuacion += 500
                        if 'ic_tc' in nombre_lc or 'ic_sdk' in nombre_lc: puntuacion += 300
                        
                        # Puntos por densidad
                        if 'xxxhdpi' in nombre_lc: puntuacion += 200
                        elif 'xxhdpi' in nombre_lc: puntuacion += 100
                        
                        if puntuacion > mejor_puntuacion:
                            mejor_puntuacion = puntuacion
                            mejor_data = data
                            print(f"📐 Candidato cuadrado encontrado: {nombre} ({ancho}x{alto}) - Score: {puntuacion}")
                            
                    except: continue
        return mejor_data
    except: return None

# ---------------------------------------------------------
# 2. SINCRONIZADOR Y LÓGICA DE EXCEL
# ---------------------------------------------------------
def sincronizar_todo(sheet):
    print("🔄 Sincronizando catálogo...")
    registros = sheet.get_all_records()
    nuevo_index = {"repo": {"name": "Mi Tienda Privada", "description": "APKs VIP", "address": REPO_URL, "icon": f"{REPO_URL}icon.png"}, "apps": []}
    apps_dict = {}
    for r in registros:
        pkg = r.get('Pkg')
        if not pkg: continue
        entry = {"versionName": str(r.get('Version')), "versionCode": str(r.get('Version Code', '0')), "downloadURL": r.get('Link APK'), "added": datetime.now().strftime("%Y-%m-%d")}
        if pkg not in apps_dict:
            apps_dict[pkg] = {"name": r.get('Nombre'), "packageName": pkg, "suggestedVersionName": str(r.get('Version')), "icon": r.get('Link Icono'), "versions": [entry]}
        else:
            if not any(v['versionName'] == entry['versionName'] for v in apps_dict[pkg]['versions']):
                apps_dict[pkg]['versions'].insert(0, entry)
    nuevo_index["apps"] = list(apps_dict.values())
    with open("index.json", "w") as f: json.dump(nuevo_index, f, indent=4)

def main():
    print("🚀 Iniciando Motor 'Arquitecto' v7...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')) for r in registros if r.get('ID Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    for item in items:
        file_id = str(item['id'])
        file_name = item['name']
        if not file_name.lower().endswith('.apk') or file_id in procesados: continue

        print(f"⚙️ Procesando: {file_name}")
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
            # USAMOS EL FILTRO CUADRADO
            icon_data = extraer_icono_arquitecto(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            
            nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
            link_apk = subir_a_dropbox(dbx, temp_apk, f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk")
            
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                link_icon = subir_a_dropbox(dbx, icon_filename, icon_filename)
                os.remove(icon_filename)

            # ORDEN DE COLUMNAS: Nombre, Estado, Link APK, Version, Pkg, Link Icono, ID Drive, Repo, Version Code
            sheet.append_row([
                nombre_limpio, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            print(f"✅ Éxito geométrico: {nombre_limpio}")

        except Exception as e: print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_todo(sheet)

def subir_a_dropbox(dbx, file_path, dest_filename):
    dest_path = f"/{dest_filename}"
    with open(file_path, "rb") as f:
        dbx.files_upload(f.read(), dest_path, mode=WriteMode('overwrite'))
    try:
        shared_link = dbx.sharing_create_shared_link_with_settings(dest_path)
        url = shared_link.url
    except:
        links = dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links
        url = links[0].url if links else None
    return url.replace("?dl=0", "?dl=1") if url else None

if __name__ == "__main__":
    main()
