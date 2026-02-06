import os
import json
import io
import shutil
import dropbox
import gspread
import zipfile
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
# 1. MOTOR DE EXTRACCIÓN EXTREMA (Legacy Finder)
# ---------------------------------------------------------
def extraer_icono_real_completo(apk_path):
    """Busca el icono de legado (el logo completo) ignorando las capas separadas."""
    try:
        apk_obj = APK(apk_path)
        # El nombre que buscamos suele ser 'ic_launcher' o 'ic_launcher_round'
        posibles_nombres = [
            os.path.basename(apk_obj.icon_info.get('path', 'ic_launcher')).split('.')[0],
            "ic_launcher",
            "ic_launcher_round",
            "app_icon"
        ]
        
        mejor_icono_data = None
        max_peso = 0

        with zipfile.ZipFile(apk_path, 'r') as z:
            nombres_archivos = z.namelist()
            
            for nombre_ref in posibles_nombres:
                # Buscamos en mipmap (donde están los logos oficiales)
                # Filtramos para que NO contenga 'foreground' ni 'background'
                candidatos = [n for n in nombres_archivos if nombre_ref in n 
                              and n.lower().endswith(('.png', '.webp')) 
                              and 'foreground' not in n.lower() 
                              and 'background' not in n.lower()]
                
                for c in candidatos:
                    try:
                        data = z.read(c)
                        img = Image.open(io.BytesIO(data))
                        w, h = img.size
                        # Un logo real de alta calidad suele ser de 144x144 para arriba
                        if w == h and w >= 144:
                            peso = z.getinfo(c).file_size
                            if peso > max_peso:
                                max_peso = peso
                                mejor_icono_data = data
                                print(f"💎 Logo completo detectado: {c} ({w}x{h})")
                    except: continue
                
                if mejor_icono_data: break # Si ya encontramos el oficial, paramos
                
        return mejor_icono_data
    except Exception as e:
        print(f"⚠️ Error en búsqueda extrema: {e}")
        return None

# ---------------------------------------------------------
# 2. SINCRONIZADOR EXCEL -> JSON
# ---------------------------------------------------------
def sincronizar_json_desde_excel(sheet):
    print("🔄 Sincronizando repositorio con el Excel...")
    registros = sheet.get_all_records()
    nuevo_index = {
        "repo": {"name": "Mi Tienda Privada", "description": "Repositorio VIP", "address": REPO_URL, "icon": f"{REPO_URL}icon.png"},
        "apps": []
    }
    apps_dict = {}
    for r in registros:
        pkg = r.get('Pkg')
        if not pkg: continue
        entry = {"versionName": str(r.get('Version')), "versionCode": str(r.get('MsgID_O_VersionCode', '0')), "downloadURL": r.get('Link_Dropbox_APK'), "added": datetime.now().strftime("%Y-%m-%d")}
        if pkg not in apps_dict:
            apps_dict[pkg] = {"name": r.get('Nombre'), "packageName": pkg, "suggestedVersionName": str(r.get('Version')), "icon": r.get('Link_Icono'), "versions": [entry]}
        else:
            if not any(v['versionName'] == entry['versionName'] for v in apps_dict[pkg]['versions']):
                apps_dict[pkg]['versions'].insert(0, entry)
    nuevo_index["apps"] = list(apps_dict.values())
    with open("index.json", "w") as f:
        json.dump(nuevo_index, f, indent=4)

# ---------------------------------------------------------
# 3. DROPBOX Y MAIN
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
    except ApiError:
        links = dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links
        url = links[0].url if links else None
    return url.replace("?dl=0", "?dl=1") if url else None

def main():
    print("🚀 Iniciando Motor de Extracción Extrema...")
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    for item in items:
        file_id, file_name = item['id'], item['name']
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

            apk_info = APK(temp_apk)
            icon_data = extraer_icono_real_completo(temp_apk) # Nuevo motor
            icon_filename = f"icon_{apk_info.package}.png"
            
            link_apk = subir_a_dropbox(dbx, temp_apk, f"{apk_info.application}_v{apk_info.version_name}.apk")
            
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                link_icon = subir_a_dropbox(dbx, icon_filename, icon_filename)
                os.remove(icon_filename)

            sheet.append_row([apk_info.application, "Publicado", link_apk, apk_info.version_name, apk_info.package, link_icon, file_id, str(apk_info.version_code)])
            print(f"✅ Éxito: {apk_info.application}")

        except Exception as e: print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_json_desde_excel(sheet)

if __name__ == "__main__":
    main()
