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

# --- CONFIGURACIÓN 100% BLINDADA ---
# Ahora SIEMPRE buscará los Secrets. Si no están, el código se detiene.
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']
REPO_URL = os.environ['REPO_URL'] # <-- Corregido: Sin URL visible, solo la variable

DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

# El JSON de Google
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# 1. MOTOR DE EXTRACCIÓN (Precisión Quirúrgica v8)
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            app_clean = app_name.lower().replace(" ", "")
            
            for nombre in archivos:
                nombre_lc = nombre.lower()
                if nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre:
                    if 'notification' in nombre_lc or 'abc_' in nombre_lc: continue
                    
                    try:
                        data = z.read(nombre)
                        img = Image.open(io.BytesIO(data))
                        w, h = img.size
                        
                        # FILTROS: Cuadrado (tol. 2px) y tamaño (120-1024px)
                        if abs(w - h) > 2: continue 
                        if not (120 <= w <= 1024): continue
                        
                        puntuacion = 0
                        
                        # -- SISTEMA DE PUNTOS --
                        if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 5000
                        if 'app_icon' in nombre_lc or 'store_icon' in nombre_lc: puntuacion += 3000
                        
                        if 'launcher' in nombre_lc:
                            puntuacion += 1000
                            if 'foreground' in nombre_lc or 'background' in nombre_lc:
                                puntuacion -= 500 
                        
                        if app_clean in nombre_lc: puntuacion += 800
                        if 'tc_' in nombre_lc: puntuacion += 400
                        
                        if 'xxxhdpi' in nombre_lc: puntuacion += 300
                        elif 'xxhdpi' in nombre_lc: puntuacion += 200
                        
                        if puntuacion > mejor_puntuacion:
                            mejor_puntuacion = puntuacion
                            mejor_data = data
                            print(f"🎯 Nuevo líder: {nombre} ({w}x{h}) - Score: {puntuacion}")
                    except: continue
        return mejor_data
    except Exception as e:
        print(f"⚠️ Error extrayendo icono: {e}")
        return None

# ---------------------------------------------------------
# 2. SINCRONIZADOR (Excel -> JSON)
# ---------------------------------------------------------
def sincronizar_todo(sheet):
    print("🔄 Sincronizando catálogo completo...")
    registros = sheet.get_all_records()
    
    # Construcción del repo usando SOLO la variable de entorno
    nuevo_index = {
        "repo": {
            "name": "Mi Tienda Privada", 
            "description": "APKs VIP", 
            "address": REPO_URL, 
            "icon": f"{REPO_URL}icon.png"
        }, 
        "apps": []
    }
    apps_dict = {}
    
    for r in registros:
        pkg = r.get('Pkg')
        if not pkg: continue
        
        entry = {
            "versionName": str(r.get('Version')),
            "versionCode": str(r.get('Version Code', '0')),
            "downloadURL": r.get('Link APK'),
            "added": datetime.now().strftime("%Y-%m-%d")
        }
        
        if pkg not in apps_dict:
            apps_dict[pkg] = {
                "name": r.get('Nombre'),
                "packageName": pkg,
                "suggestedVersionName": str(r.get('Version')),
                "icon": r.get('Link Icono'),
                "versions": [entry]
            }
        else:
            if not any(v['versionName'] == entry['versionName'] for v in apps_dict[pkg]['versions']):
                apps_dict[pkg]['versions'].insert(0, entry)

    nuevo_index["apps"] = list(apps_dict.values())
    with open("index.json", "w") as f:
        json.dump(nuevo_index, f, indent=4)
    print("✅ index.json generado exitosamente.")

# ---------------------------------------------------------
# 3. FUNCIONES DROPBOX
# ---------------------------------------------------------
def conectar_dropbox():
    return dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)

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
    
    if url:
        return url.replace("?dl=0", "?dl=1")
    return None

# ---------------------------------------------------------
# 4. MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor Blindado (v11 - Secure)...")
    
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    for item in items:
        file_id = str(item['id']).strip()
        file_name = item['name']
        
        if not file_name.lower().endswith('.apk'): continue
        if file_id in procesados:
            print(f"⏩ Saltando {file_name}")
            continue

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
            
            # Limpieza de nombre
            nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
            
            icon_data = extraer_icono_precision(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            
            nombre_final_apk = f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final_apk)
            
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                url_subida = subir_a_dropbox(dbx, icon_filename, icon_filename)
                if url_subida: link_icon = url_subida
                os.remove(icon_filename)

            sheet.append_row([
                nombre_limpio, 
                "Publicado", 
                link_apk, 
                apk.version_name, 
                apk.package, 
                link_icon, 
                file_id, 
                "Dropbox/Repo", 
                str(apk.version_code)
            ])
            print(f"✅ Éxito: {nombre_limpio}")

        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_todo(sheet)

if __name__ == "__main__":
    main()
