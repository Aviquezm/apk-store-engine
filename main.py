import os
import json
import io
import shutil
import dropbox
import gspread
import zipfile
from datetime import datetime
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pyaxmlparser import APK 

# --- CONFIGURACIÓN SEGURA ---
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']
REPO_URL = os.environ.get('REPO_URL', 'https://aviquezm.github.io/apk-store-engine/') 

# Credenciales Dropbox
DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

# Credenciales Google
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# 1. ESCÁNER DE RECURSOS PROFUNDO (Anti-Shazam)
# ---------------------------------------------------------
def extraer_icono_profundo(apk_path, package_name):
    """
    Busca quirúrgicamente el icono de mejor calidad ignorando archivos XML.
    """
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            nombres = z.namelist()
            apk_obj = APK(apk_path)
            icono_path_bruto = apk_obj.icon_info.get('path')
            
            candidatos = []
            if icono_path_bruto:
                # Extraemos el nombre base (ej: 'ic_launcher')
                base_name = os.path.basename(icono_path_bruto).split('.')[0]
                # Buscamos archivos que se llamen igual pero sean imágenes reales
                candidatos = [n for n in nombres if base_name in n and n.lower().endswith(('.png', '.webp'))]

            # Si no hay candidatos directos, buscamos patrones universales
            if not candidatos:
                patrones = ['ic_launcher', 'app_icon', 'icon']
                for p in patrones:
                    candidatos += [n for n in nombres if p in n.lower() and n.lower().endswith(('.png', '.webp'))]

            if candidatos:
                # Eliminamos duplicados y ordenamos por tamaño de archivo (mayor peso = mejor calidad)
                candidatos = list(set(candidatos))
                candidatos.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                
                # Priorizamos carpetas de alta densidad (xxxhdpi)
                prioridad = [n for n in candidatos if any(dpi in n for dpi in ['xxxhdpi', 'xxhdpi', 'xhdpi'])]
                ganador = prioridad[0] if prioridad else candidatos[0]
                
                print(f"🎯 Icono de alta calidad encontrado: {ganador} ({z.getinfo(ganador).file_size} bytes)")
                return z.read(ganador)
                
        return None
    except Exception as e:
        print(f"⚠️ Error en extracción profunda: {e}")
        return None

def analizar_apk(apk_path):
    try:
        apk = APK(apk_path)
        package_name = apk.package
        
        # Invocamos al nuevo motor de extracción profunda
        icon_data = extraer_icono_profundo(apk_path, package_name)
        icon_filename = f"icon_{package_name}.png"
        
        if icon_data:
            with open(icon_filename, "wb") as f:
                f.write(icon_data)
        else:
            icon_filename = None 

        return {
            "pkg": package_name,
            "ver_name": apk.version_name,
            "ver_code": apk.version_code,
            "name": apk.application,
            "icon_file": icon_filename
        }
    except Exception as e:
        print(f"❌ Error analizando APK: {e}")
        return None

# ---------------------------------------------------------
# 2. LÓGICA DROPBOX
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
        if links: url = links[0].url
        else: return None
    return url.replace("?dl=0", "?dl=1").replace("&dl=0", "&dl=1")

# ---------------------------------------------------------
# 3. GENERADOR DE REPO (CON ANTI-DUPLICADOS)
# ---------------------------------------------------------
def actualizar_index_json(nuevo_dato):
    archivo_repo = "index.json"
    datos_repo = {"repo": {"name": "Mi Tienda Privada", "description": "APKs desde Google Drive", "address": REPO_URL, "icon": f"{REPO_URL}icon.png"}, "apps": []}

    if os.path.exists(archivo_repo):
        try:
            with open(archivo_repo, "r") as f:
                datos_repo = json.load(f)
        except: pass

    app_encontrada = False
    nueva_entry = {
        "versionName": nuevo_dato["ver_name"],
        "versionCode": str(nuevo_dato["ver_code"]),
        "downloadURL": nuevo_dato["link_apk"],
        "size": 0,
        "added": datetime.now().strftime("%Y-%m-%d")
    }

    for app in datos_repo["apps"]:
        if app["packageName"] == nuevo_dato["pkg"]:
            app_encontrada = True
            app["icon"] = nuevo_dato["link_icon"]
            app["suggestedVersionName"] = nuevo_dato["ver_name"]
            app["suggestedVersionCode"] = str(nuevo_dato["ver_code"])
            # Filtro para no repetir la misma versión en la lista
            if not any(v["versionCode"] == str(nuevo_dato["ver_code"]) for v in app["versions"]):
                app["versions"].insert(0, nueva_entry)
            break
    
    if not app_encontrada:
        app_completa = {
            "name": nuevo_dato["name"],
            "packageName": nuevo_dato["pkg"],
            "suggestedVersionName": nuevo_dato["ver_name"],
            "suggestedVersionCode": str(nuevo_dato["ver_code"]),
            "icon": nuevo_dato["link_icon"],
            "web": nuevo_dato["link_apk"],
            "versions": [nueva_entry]
        }
        datos_repo["apps"].append(app_completa)

    with open(archivo_repo, "w") as f:
        json.dump(datos_repo, f, indent=4)
    print("✅ index.json actualizado")

# ---------------------------------------------------------
# 4. MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor (Modo Extracción Profunda)...")
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
        if not file_name.lower().endswith('.apk'): continue
        if file_id in procesados: continue

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

            info = analizar_apk(temp_apk)
            if not info or not info['pkg']: continue
            
            nombre_final = f"{info['name'].replace(' ', '_')}_v{info['ver_name']}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
            
            link_icon = "https://via.placeholder.com/150"
            if info['icon_file'] and os.path.exists(info['icon_file']):
                link_icon = subir_a_dropbox(dbx, info['icon_file'], f"icon_{info['pkg']}.png")
                os.remove(info['icon_file'])

            actualizar_index_json({
                "pkg": info['pkg'], "name": info['name'],
                "ver_name": info['ver_name'], "ver_code": info['ver_code'],
                "link_apk": link_apk, "link_icon": link_icon
            })

            sheet.append_row([info['name'], "Publicado", link_apk, info['ver_name'], info['pkg'], link_icon, file_id, "Dropbox/Repo"])
            print(f"✅ Éxito: {info['name']}")

        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

if __name__ == "__main__":
    main()
