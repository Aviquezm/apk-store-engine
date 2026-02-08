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
# 1. MOTOR DE EXTRACCIÓN (Calibrado para Truecaller/Shazam)
# ---------------------------------------------------------
def extraer_icono_rebelde(apk_path, app_name):
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            app_clean = app_name.lower().replace(" ", "")
            
            # A. RADAR POR NOMBRE (Truecaller usa 'tc_' o 'truecaller')
            radar = [n for n in archivos if (app_clean in n.lower() or 'tc_' in n.lower()) and 'icon' in n.lower() and n.endswith(('.png', '.webp'))]
            if radar:
                radar.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                return z.read(radar[0])

            # B. FUSIÓN DE CAPAS (Para escudos modernos)
            for d in ['xxxhdpi', 'xxhdpi', 'xhdpi']:
                fg = [n for n in archivos if d in n and 'ic_launcher' in n and 'foreground' in n and n.endswith(('.png', '.webp'))]
                bg = [n for n in archivos if d in n and 'ic_launcher' in n and 'background' in n and n.endswith(('.png', '.webp'))]
                if fg and bg:
                    img_bg = Image.open(io.BytesIO(z.read(bg[0]))).convert("RGBA")
                    img_fg = Image.open(io.BytesIO(z.read(fg[0]))).convert("RGBA")
                    if img_bg.size != img_fg.size: img_fg = img_fg.resize(img_bg.size, Image.LANCZOS)
                    img_bg.paste(img_fg, (0, 0), img_fg)
                    out = io.BytesIO()
                    img_bg.save(out, format="PNG")
                    return out.getvalue()

            # C. LEGADO
            launchers = [n for n in archivos if 'ic_launcher' in n and n.endswith(('.png', '.webp')) and 'foreground' not in n.lower()]
            if launchers:
                launchers.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                return z.read(launchers[0])
        return None
    except: return None

# ---------------------------------------------------------
# 2. SINCRONIZADOR EXCEL -> JSON (Orden estricto)
# ---------------------------------------------------------
def sincronizar_todo(sheet):
    print("🔄 Sincronizando catálogo desde Excel...")
    registros = sheet.get_all_records()
    nuevo_index = {"repo": {"name": "Mi Tienda Privada", "description": "APKs VIP", "address": REPO_URL, "icon": f"{REPO_URL}icon.png"}, "apps": []}
    apps_dict = {}
    
    for r in registros:
        # Usamos los nombres exactos de tus columnas (con espacios)
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

# ---------------------------------------------------------
# 3. MAIN (Lógica de procesados corregida)
# ---------------------------------------------------------
def main():
    print("🚀 Motor Gladiador v4 (Houston, tenemos el control)...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    # IMPORTANTE: Usamos 'ID Drive' con espacio para coincidir con tu Excel
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')) for r in registros if r.get('ID Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    for item in items:
        file_id, file_name = item['id'], item['name']
        if not file_name.lower().endswith('.apk'): continue
        
        # Si el ID ya está en el Excel, lo saltamos
        if file_id in procesados: 
            print(f"⏩ Saltando (Ya existe): {file_name}")
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
            icon_data = extraer_icono_rebelde(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            
            # Subir a Dropbox
            link_apk = subir_a_dropbox(dbx, temp_apk, f"{apk.application.replace(' ', '_')}_v{apk.version_name}.apk")
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                link_icon = subir_a_dropbox(dbx, icon_filename, icon_filename)
                os.remove(icon_filename)

            # ORDEN DE COLUMNAS SOLICITADO:
            # 1.Nombre | 2.Estado | 3.Link APK | 4.Version | 5.Pkg | 6.Link Icono | 7.ID Drive | 8.Repo | 9.Version Code
            sheet.append_row([
                apk.application, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            print(f"✅ Éxito: {apk.application}")

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
