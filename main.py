import os
import json
import gspread
import io
import asyncio
import subprocess
import re
import zipfile
import shutil
import dropbox
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# --- CONFIGURACIÓN ---
ADMIN_ID = int(os.environ['ADMIN_ID'])
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']

# Credenciales Dropbox (Sistema de Token Infinito)
DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# FUNCIONES AUXILIARES
# ---------------------------------------------------------
def obtener_info_aapt(apk_path):
    try:
        cmd = ['aapt', 'dump', 'badging', apk_path]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        pkg = re.search(r"package: name='([^']+)'", res.stdout).group(1)
        ver = re.search(r"versionCode='([^']+)'", res.stdout).group(1)
        label_m = re.search(r"application-label:'([^']+)'", res.stdout)
        label = label_m.group(1) if label_m else pkg
        return pkg, ver, label
    except: return None, None, None

def cazar_icono_real(apk_path):
    # Lógica simplificada de extracción
    try:
        cmd = ['aapt', 'dump', 'badging', apk_path]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
        icon_entries = re.findall(r"application-icon-\d+:'([^']+)'", out)
        default_icon = re.search(r"icon='([^']+)'", out)
        if default_icon: icon_entries.append(default_icon.group(1))
        
        with zipfile.ZipFile(apk_path, 'r') as z:
            nombres = z.namelist()
            for icon_path in icon_entries:
                if icon_path in nombres and icon_path.lower().endswith(('.png','.webp')): return icon_path
            # Búsqueda laxa
            candidatos = [n for n in nombres if ('launcher' in n or 'icon' in n) and n.lower().endswith(('.png','.webp'))]
            if candidatos:
                candidatos.sort(key=lambda x: z.getinfo(x).file_size, reverse=True)
                return candidatos[0]
    except: pass
    return None

# ---------------------------------------------------------
# LÓGICA DROPBOX (Limpieza y Subida)
# ---------------------------------------------------------
def conectar_dropbox():
    """Conecta usando el Refresh Token para que nunca caduque"""
    return dropbox.Dropbox(
        app_key=DBX_KEY,
        app_secret=DBX_SECRET,
        oauth2_refresh_token=DBX_REFRESH_TOKEN
    )

def limpiar_versiones_viejas(dbx, label_app):
    """Busca y borra archivos antiguos de la misma app para ahorrar espacio"""
    try:
        archivos = dbx.files_list_folder('').entries
        for archivo in archivos:
            # Si el archivo se llama igual (ej: 'Spotify') pero es versión vieja...
            if isinstance(archivo, dropbox.files.FileMetadata):
                # Comparamos el inicio del nombre (ej: "Spotify_v")
                nombre_base = label_app.replace(" ", "_")
                if archivo.name.startswith(nombre_base + "_v"):
                    print(f"🗑️ Borrando versión vieja: {archivo.name}")
                    dbx.files_delete_v2(archivo.path_lower)
    except Exception as e:
        print(f"Nota sobre limpieza: {e}")

def subir_y_obtener_link(dbx, file_path, dest_filename):
    """Sube y devuelve link directo (dl=1)"""
    dest_path = f"/{dest_filename}"
    
    # Subir
    with open(file_path, "rb") as f:
        dbx.files_upload(f.read(), dest_path, mode=WriteMode('overwrite'))
    
    # Crear Link
    try:
        shared_link = dbx.sharing_create_shared_link_with_settings(dest_path)
        url = shared_link.url
    except ApiError:
        # Si ya existe, lo recuperamos
        links = dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links
        if links: url = links[0].url
        else: return None
        
    # Convertir a descarga directa
    return url.replace("?dl=0", "?dl=1").replace("&dl=0", "&dl=1")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor Dropbox (Con Auto-Limpieza)...")
    
    # 1. Conexiones
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')}
    
    # Buscamos APKs nuevas en Drive
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    for item in items:
        file_id, file_name = item['id'], item['name']
        if not file_name.lower().endswith('.apk'): continue
        if file_id in procesados: continue

        print(f"⚙️ Procesando: {file_name}")
        temp_apk, final_icon = "temp.apk", "icon_final.png"
        
        try:
            # A. Descargar de Drive
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open(temp_apk, "wb") as f: f.write(fh.read())

            # B. Analizar
            pkg, ver, label = obtener_info_aapt(temp_apk)
            if not pkg: continue
            
            # C. Extraer Icono (Para subirlo también y tener link directo)
            ruta_icono = cazar_icono_real(temp_apk)
            icon_filename = f"{pkg}_icon.png"
            if ruta_icono:
                with zipfile.ZipFile(temp_apk, 'r') as z:
                    with z.open(ruta_icono) as src, open(icon_filename, "wb") as trg:
                        trg.write(src.read())
            else:
                shutil.copy("default_icon.png", icon_filename)

            # D. SUBIDA INTELIGENTE A DROPBOX
            print("🧹 Limpiando versiones viejas...")
            limpiar_versiones_viejas(dbx, label)
            
            print("☁️ Subiendo archivos nuevos...")
            nombre_apk_final = f"{label.replace(' ', '_')}_v{ver}.apk"
            
            link_apk = subir_y_obtener_link(dbx, temp_apk, nombre_apk_final)
            link_icon = subir_y_obtener_link(dbx, icon_filename, icon_filename)
            
            print(f"✅ Éxito! Link: {link_apk}")

            # E. Guardar en Excel
            # IMPORTANTE: Guardamos el LINK DIRECTO en la columna 'Notas' (Columna C)
            # Estructura: [Nombre, Estado, Link_Dropbox, Version, Pkg, Link_Icono, DriveID, "Dropbox"]
            sheet.append_row([
                label, 
                "Publicado", 
                link_apk,     # Columna C (Notas) ahora guarda el Link APK
                ver, 
                pkg, 
                link_icon,    # Columna F (MsgID) ahora guarda Link Icono
                file_id, 
                "Dropbox"
            ])

        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)
            if os.path.exists(icon_filename): os.remove(icon_filename)

if __name__ == "__main__":
    main()
