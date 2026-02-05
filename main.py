import os
import json
import io
import shutil
import dropbox
import gspread
from datetime import datetime
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from androguard.core.bytecodes.apk import APK # USAMOS ESTO EN LUGAR DE AAPT

# --- CONFIGURACIÓN ---
# Variables de entorno cargadas desde GitHub Secrets
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']
REPO_URL = "https://aviquezm.github.io/apk-store-engine/" # TU URL DE GITHUB PAGES

# Credenciales Dropbox
DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

# Credenciales Google
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# 1. FUNCIONES AUXILIARES (MODIFICADO PARA ANDROGUARD)
# ---------------------------------------------------------
def analizar_apk(apk_path):
    """Extrae info y el icono usando Androguard (Funciona en la nube)"""
    try:
        apk = APK(apk_path)
        package_name = apk.get_package()
        version_name = apk.get_androidversion_name()
        version_code = apk.get_androidversion_code()
        app_name = apk.get_app_name()
        
        # Extraer icono a archivo físico
        icon_data = apk.get_icon()
        icon_filename = f"icon_{package_name}.png"
        
        if icon_data:
            with open(icon_filename, "wb") as f:
                f.write(icon_data)
        else:
            icon_filename = None # O manejar un default

        return {
            "pkg": package_name,
            "ver_name": version_name,
            "ver_code": version_code,
            "name": app_name,
            "icon_file": icon_filename
        }
    except Exception as e:
        print(f"Error analizando APK: {e}")
        return None

# ---------------------------------------------------------
# 2. LÓGICA DROPBOX
# ---------------------------------------------------------
def conectar_dropbox():
    return dropbox.Dropbox(
        app_key=DBX_KEY,
        app_secret=DBX_SECRET,
        oauth2_refresh_token=DBX_REFRESH_TOKEN
    )

def subir_a_dropbox(dbx, file_path, dest_filename):
    """Sube y devuelve link directo (dl=1)"""
    dest_path = f"/{dest_filename}"
    with open(file_path, "rb") as f:
        dbx.files_upload(f.read(), dest_path, mode=WriteMode('overwrite'))
    
    try:
        shared_link = dbx.sharing_create_shared_link_with_settings(dest_path)
        url = shared_link.url
    except ApiError:
        links = dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links
        if links: url = links[0].url
        else: return None
        
    return url.replace("?dl=0", "?dl=1").replace("&dl=0", "&dl=1")

# ---------------------------------------------------------
# 3. GENERADOR DE REPO (LO NUEVO)
# ---------------------------------------------------------
def actualizar_index_json(nuevo_dato):
    """Lee el index.json actual, agrega la app y guarda"""
    archivo_repo = "index.json"
    
    # Estructura base si no existe el archivo
    datos_repo = {
        "repo": {
            "name": "Mi Tienda Privada",
            "description": "APKs desde Google Drive",
            "address": REPO_URL,
            "icon": f"{REPO_URL}icon.png" 
        },
        "apps": []
    }

    if os.path.exists(archivo_repo):
        try:
            with open(archivo_repo, "r") as f:
                datos_repo = json.load(f)
        except: pass # Si falla, usamos la base vacía

    # Buscar si la app ya existe para actualizarla, o crearla si es nueva
    app_encontrada = False
    nueva_entry = {
        "versionName": nuevo_dato["ver_name"],
        "versionCode": str(nuevo_dato["ver_code"]),
        "downloadURL": nuevo_dato["link_apk"],
        "size": 0, # Opcional
        "added": datetime.now().strftime("%Y-%m-%d") # Importante para que Droid-ify vea que es nuevo
    }

    for app in datos_repo["apps"]:
        if app["packageName"] == nuevo_dato["pkg"]:
            app_encontrada = True
            # Actualizamos datos generales
            app["icon"] = nuevo_dato["link_icon"] # Usamos el link de Dropbox
            app["suggestedVersionName"] = nuevo_dato["ver_name"]
            app["suggestedVersionCode"] = str(nuevo_dato["ver_code"])
            # Agregamos la versión a la lista de versiones
            app["versions"].insert(0, nueva_entry) # Poner la más nueva primero
            break
    
    if not app_encontrada:
        # Estructura de app nueva para Droid-ify
        app_completa = {
            "name": nuevo_dato["name"],
            "packageName": nuevo_dato["pkg"],
            "suggestedVersionName": nuevo_dato["ver_name"],
            "suggestedVersionCode": str(nuevo_dato["ver_code"]),
            "icon": nuevo_dato["link_icon"], # Link de Dropbox
            "web": nuevo_dato["link_apk"],
            "versions": [nueva_entry]
        }
        datos_repo["apps"].append(app_completa)

    # Guardar cambios
    with open(archivo_repo, "w") as f:
        json.dump(datos_repo, f, indent=4)
    print("✅ index.json actualizado para Droid-ify")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor...")
    
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    nuevos_procesados = False

    for item in items:
        file_id, file_name = item['id'], item['name']
        if not file_name.lower().endswith('.apk'): continue
        if file_id in procesados: continue

        print(f"⚙️ Procesando: {file_name}")
        temp_apk = "temp.apk"
        
        try:
            # A. Descargar
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open(temp_apk, "wb") as f: f.write(fh.read())

            # B. Analizar (con Androguard)
            info = analizar_apk(temp_apk)
            if not info: continue
            
            # C. Subir a Dropbox (APK e Icono)
            nombre_final = f"{info['name'].replace(' ', '_')}_v{info['ver_name']}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
            
            link_icon = "https://via.placeholder.com/150" # Default por si falla
            if info['icon_file']:
                link_icon = subir_a_dropbox(dbx, info['icon_file'], f"icon_{info['pkg']}.png")
                os.remove(info['icon_file'])

            # D. Actualizar index.json (Para Droid-ify)
            datos_para_repo = {
                "pkg": info['pkg'],
                "name": info['name'],
                "ver_name": info['ver_name'],
                "ver_code": info['ver_code'],
                "link_apk": link_apk,
                "link_icon": link_icon
            }
            actualizar_index_json(datos_para_repo)
            nuevos_procesados = True

            # E. Guardar en Excel (Tu log original)
            sheet.append_row([
                info['name'], "Publicado", link_apk, info['ver_name'], 
                info['pkg'], link_icon, file_id, "Dropbox/Repo"
            ])
            
            print(f"✅ Completado: {info['name']}")

        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

if __name__ == "__main__":
    main()
