import os
import json
import io
import shutil
import dropbox
import gspread
import zipfile
from datetime import datetime
from PIL import Image # Librería para análisis visual
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
# 1. MOTOR DE BÚSQUEDA VISUAL (Fuerza Bruta)
# ---------------------------------------------------------
def buscar_icono_por_fuerza_bruta(apk_path):
    """
    Escanea TODO el APK buscando la imagen cuadrada más grande y pesada.
    """
    mejor_icono_data = None
    max_peso = 0

    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            for nombre in z.namelist():
                # Solo nos interesan imágenes PNG o WebP
                if nombre.lower().endswith(('.png', '.webp')) and 'res/' in nombre:
                    try:
                        data = z.read(nombre)
                        img = Image.open(io.BytesIO(data))
                        ancho, alto = img.size
                        
                        # CONDICIÓN: Debe ser cuadrada (1:1) y de un tamaño decente
                        if ancho == alto and 90 <= ancho <= 700:
                            peso = z.getinfo(nombre).file_size
                            # Nos quedamos con la más pesada (suele ser la de mayor calidad)
                            if peso > max_peso:
                                max_peso = peso
                                mejor_icono_data = data
                                print(f"🔍 Candidato encontrado: {nombre} ({ancho}x{alto})")
                    except:
                        continue
        return mejor_icono_data
    except Exception as e:
        print(f"⚠️ Error en búsqueda visual: {e}")
        return None

def analizar_apk(apk_path):
    try:
        apk = APK(apk_path)
        package_name = apk.package
        
        # Primero intentamos la extracción normal
        icon_data = apk.icon_data
        
        # Si falla o es XML, activamos el ESCÁNER VISUAL
        if not icon_data:
            print(f"🚀 Activando escáner visual para {apk.application}...")
            icon_data = buscar_icono_por_fuerza_bruta(apk_path)
        
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
        print(f"❌ Error crítico: {e}")
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
# 3. GENERADOR DE REPO
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
    print("🚀 Iniciando Motor (Modo Fuerza Bruta Visual)...")
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
