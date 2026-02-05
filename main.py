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
# 1. FUNCIÓN DE ANÁLISIS (MODO CAZADOR REBELDE)
# ---------------------------------------------------------
def analizar_apk(apk_path):
    try:
        apk = APK(apk_path)
        package_name = apk.package
        version_name = apk.version_name
        version_code = apk.version_code
        app_name = apk.application
        
        icon_filename = f"icon_{package_name}.png"
        icon_data = apk.icon_data # Intento normal
        
        # PLAN B: Si pyaxmlparser falla (caso Shazam)
        if not icon_data:
            print(f"🕵️ Buscando icono rebelde en {app_name}...")
            with zipfile.ZipFile(apk_path, 'r') as z:
                # Buscamos archivos con nombres comunes de iconos
                candidatos = [n for n in z.namelist() if 'ic_launcher' in n or 'app_icon' in n]
                # Priorizamos los que son PNG o WebP y de carpetas de alta resolución
                candidatos = [c for c in candidatos if c.endswith(('.png', '.webp')) and 'res/' in c]
                if candidatos:
                    # Ordenamos para intentar agarrar el de mejor calidad (usualmente el más pesado)
                    candidatos.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                    icon_data = z.read(candidatos[0])
                    print(f"🎯 Icono encontrado manualmente en: {candidatos[0]}")

        if icon_data:
            with open(icon_filename, "wb") as f:
                f.write(icon_data)
        else:
            icon_filename = None 

        return {
            "pkg": package_name,
            "ver_name": version_name,
            "ver_code": version_code,
            "name": app_name,
            "icon_file": icon_filename
        }
    except Exception as e:
        print(f"❌ Error analizando APK: {e}")
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
            # EVITAR DUPLICADOS DE VERSIÓN
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
    print("🚀 Iniciando Motor (Modo Cazador de Iconos)...")
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
