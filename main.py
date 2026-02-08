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
# 1. MOTOR DE EXTRACCIÓN AVANZADA (Radar de Logos)
# ---------------------------------------------------------
def extraer_logo_definitivo(apk_path, app_name):
    """
    Busca el icono real usando jerarquía, fusión y radar de nombres.
    """
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            app_name_clean = app_name.lower().replace(" ", "")
            
            # PASO 1: Radar de Nombres (Específico para Shazam y similares)
            # Busca imágenes que tengan el nombre de la app + 'icon' o 'logo'
            radar = [n for n in archivos if app_name_clean in n.lower() and 'icon' in n.lower() and n.endswith(('.png', '.webp'))]
            if radar:
                radar.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                print(f"🎯 Radar detectó logo específico: {radar[0]}")
                return z.read(radar[0])

            # PASO 2: Intentar fusión de capas (Fondo + Frente)
            for d in ['xxxhdpi', 'xxhdpi', 'xhdpi']:
                fg = [n for n in archivos if d in n and 'foreground' in n.lower() and n.endswith(('.png', '.webp'))]
                bg = [n for n in archivos if d in n and 'background' in n.lower() and n.endswith(('.png', '.webp'))]
                if fg and bg:
                    print(f"🧩 Fusionando capas en {d}...")
                    img_bg = Image.open(io.BytesIO(z.read(bg[0]))).convert("RGBA")
                    img_fg = Image.open(io.BytesIO(z.read(fg[0]))).convert("RGBA")
                    if img_bg.size != img_fg.size: img_fg = img_fg.resize(img_bg.size, Image.LANCZOS)
                    img_bg.paste(img_fg, (0, 0), img_fg)
                    out = io.BytesIO()
                    img_bg.save(out, format="PNG")
                    return out.getvalue()

            # PASO 3: Buscar cualquier 'ic_launcher' que sea imagen (Legado)
            launchers = [n for n in archivos if 'ic_launcher' in n and n.endswith(('.png', '.webp')) and 'foreground' not in n.lower()]
            if launchers:
                launchers.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                print(f"💎 Logo legado encontrado: {launchers[0]}")
                return z.read(launchers[0])

        return None
    except Exception as e:
        print(f"⚠️ Error en motor: {e}")
        return None

# ---------------------------------------------------------
# 2. SINCRONIZADOR EXCEL -> JSON
# ---------------------------------------------------------
def sincronizar_json_desde_excel(sheet):
    print("🔄 Sincronizando index.json...")
    registros = sheet.get_all_records()
    nuevo_index = {"repo": {"name": "Mi Tienda Privada", "description": "APKs VIP", "address": REPO_URL, "icon": f"{REPO_URL}icon.png"}, "apps": []}
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
    with open("index.json", "w") as f: json.dump(nuevo_index, f, indent=4)

# ---------------------------------------------------------
# 3. LÓGICA DROPBOX Y MAIN
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
    print("🚀 Iniciando Motor 'Radar de Logos'...")
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
            icon_data = extraer_logo_definitivo(temp_apk, apk_info.application) # USANDO EL RADAR
            icon_filename = f"icon_{apk_info.package}.png"
            
            link_apk = subir_a_dropbox(dbx, temp_apk, f"{apk_info.application.replace(' ', '_')}_v{apk_info.version_name}.apk")
            
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                link_icon = subir_a_dropbox(dbx, icon_filename, icon_filename)
                os.remove(icon_filename)

            sheet.append_row([apk_info.application, "Publicado", link_apk, apk_info.version_name, apk_info.package, link_icon, file_id, str(apk_info.version_code)])
            print(f"✅ Éxito con radar: {apk_info.application}")

        except Exception as e: print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_json_desde_excel(sheet)

if __name__ == "__main__":
    main()
