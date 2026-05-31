import os
import json
import io
import time
import hashlib
import dropbox
import gspread
import zipfile
import re
import requests
from datetime import datetime
from PIL import Image
from dropbox.files import WriteMode
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from pyaxmlparser import APK

# --- CONFIGURACIÓN ---
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']
REPO_URL_BASE = "https://aviquezm.github.io/apk-store-engine/"

DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[WARN] Error enviando notificación: {e}")

def calcular_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(4096), b""):
            sha256.update(block)
    return sha256.hexdigest()

def nombre_seguro(texto):
    return re.sub(r'[^a-zA-Z0-9]', '_', str(texto).strip().lower())

def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            for nombre in z.namelist():
                if not (nombre.lower().endswith(('.png', '.webp')) and 'res/' in nombre):
                    continue
                if 'notification' in nombre.lower():
                    continue
                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    if w < 48:
                        continue
                    puntuacion = 0
                    if 'ic_launcher' in nombre:
                        puntuacion += 500
                    if w > 100:
                        puntuacion += 100
                    if puntuacion > mejor_puntuacion:
                        mejor_puntuacion = puntuacion
                        mejor_data = data
                except:
                    continue
            return mejor_data
    except Exception as e:
        print(f"[WARN] Error extrayendo icono: {e}")
        return None

# ---------------------------------------------------------
# LIMPIEZA
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        pkg_nuevo = str(pkg_nuevo_raw).strip().lower()
        
        for i, r in enumerate(registros):
            pkg_viejo = str(r.get('Pkg')).strip().lower()
            id_viejo = str(r.get('ID Drive', '')).strip()
            
            if pkg_viejo == pkg_nuevo and id_viejo != id_archivo_nuevo:
                try:
                    drive_service.files().delete(fileId=id_viejo).execute()
                except Exception as e:
                    print(f"[WARN] No se pudo borrar de Drive: {e}")
                
                try:
                    dbx.files_delete_v2(f"/{r.get('Nombre', '').replace(' ', '_')}_v{r.get('Version', '')}.apk")
                except Exception as e:
                    print(f"[WARN] No se pudo borrar de Dropbox: {e}")
                
                filas_a_borrar.append(i + 2)
                time.sleep(0.5)
        
        if filas_a_borrar:
            for fila_num in sorted(filas_a_borrar, reverse=True):
                try:
                    sheet.delete_row(fila_num)
                    time.sleep(1.5)
                except Exception as e:
                    print(f"[WARN] Error borrando fila {fila_num}: {e}")
    except Exception as e:
        print(f"[ERROR] Error en limpieza: {e}")

# ---------------------------------------------------------
# GENERADOR V37 (FIX DROPBOX URL + REGEX PERMISIVAS)
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("🔄 Generando Sistema V37...")
    registros = sheet.get_all_records()
    obtainium_apps = []

    for r in registros:
        if not r.get('Pkg'):
            continue
        
        # --- BLINDAJE DE DATOS ---
        nombre = str(r.get('Nombre', 'App')).strip()
        version = str(r.get('Version', '1.0')).strip()
        link_apk = str(r.get('Link APK', '')).strip()
        pkg = str(r.get('Pkg', '')).strip()
        
        # HTML Individual con cache-busting
        filename = f"{nombre_seguro(nombre)}.html"
        cache_buster = int(time.time())
        full_url = f"{REPO_URL_BASE}{filename}?v={cache_buster}"
        
        # HTML SUPER SIMPLE - Obtainium detecta fácilmente
        html_content = f"""<!DOCTYPE html>
<html>
<head><title>{nombre}</title></head>
<body>
<h1>{nombre}</h1>
<p>Version: {version}</p>
<a href="{link_apk}">Download</a>
</body>
</html>"""
        
        with open(filename, "w", encoding='utf-8') as f:
            f.write(html_content)
        
        # ESTRUCTURA PARA OBTAINIUM - Regex que maneja ?dl=1 de Dropbox
        app_entry = {
            "url": full_url,
            "appName": nombre,
            "appAuthor": pkg,
            "additionalSettings": {
                "sourceType": "html",
                "versionRegex": r"[Vv]ersion[:\s]+([\d\.]+)",
                "downloadLinkRegex": r'href=["\']([^"\']+\.apk[^"\']*)["\']',
                "pollingIntervalMinutes": 30
            }
        }
        
        obtainium_apps.append(app_entry)

    # GUARDADO: ARRAY DIRECTO (formato Bulk Import de Obtainium)
    with open("obtainium.json", "w", encoding='utf-8') as f:
        json.dump(obtainium_apps, f, indent=2, ensure_ascii=False)
    
    # Validar JSON
    try:
        with open("obtainium.json", "r") as f:
            validado = json.load(f)
            print(f"✅ JSON válido: {len(validado)} apps configuradas")
    except Exception as e:
        print(f"[ERROR] JSON inválido: {e}")
    
    # Índice con versión visible
    with open("index.html", "w", encoding='utf-8') as f:
        f.write(f"<html><body><h1>V37 Online - Tienda APK</h1><p>Apps: {len(obtainium_apps)}</p></body></html>")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor V37...")
    
    try:
        dbx = dropbox.Dropbox(
            app_key=DBX_KEY,
            app_secret=DBX_SECRET,
            oauth2_refresh_token=DBX_REFRESH_TOKEN
        )
        creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
        drive_service = build('drive', 'v3', credentials=creds)
        client_gs = gspread.authorize(creds)
        sheet = client_gs.open_by_key(SHEET_ID).sheet1
        
        registros = sheet.get_all_records()
        procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
        
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items = drive_service.files().list(
            q=query,
            fields="files(id, name)"
        ).execute().get('files', [])
        
        nuevos = [
            i for i in items
            if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados
        ]

        if nuevos:
            notificar(f"👷‍♂️ <b>Procesando {len(nuevos)} APKs</b>")
            
            for item in nuevos:
                temp_apk = "temp.apk"
                try:
                    # Descargar APK de Drive
                    request = drive_service.files().get_media(fileId=item['id'])
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                    
                    fh.seek(0)
                    with open(temp_apk, "wb") as f:
                        f.write(fh.read())

                    # Parsear APK
                    apk = APK(temp_apk)
                    nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                    icon_data = extraer_icono_precision(temp_apk, apk.application)
                    
                    # Subir icono a Dropbox
                    link_icon = "https://via.placeholder.com/64"
                    if icon_data:
                        with open("temp.png", "wb") as f:
                            f.write(icon_data)
                        with open("temp.png", "rb") as f:
                            dbx.files_upload(
                                f.read(),
                                f"/icon_{apk.package}.png",
                                mode=WriteMode('overwrite')
                            )
                        l = dbx.sharing_create_shared_link_with_settings(f"/icon_{apk.package}.png").url
                        link_icon = l.replace("?dl=0", "?dl=1")
                        os.remove("temp.png")

                    # Subir APK a Dropbox
                    path = f"/{nombre}_{apk.version_name}.apk"
                    with open(temp_apk, "rb") as f:
                        dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("?dl=0", "?dl=1")

                    # Agregar a Sheets
                    sheet.append_row([
                        nombre,
                        "Publicado",
                        link_apk,
                        apk.version_name,
                        apk.package,
                        link_icon,
                        item['id'],
                        "Dropbox",
                        str(apk.version_code),
                        calcular_hash(temp_apk),
                        str(os.path.getsize(temp_apk))
                    ])
                    
                    # Limpiar versiones anteriores
                    eliminar_rastros_anteriores(sheet, drive_service, dbx, apk.package, item['id'])
                    
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"[ERROR] Procesando {item['name']}: {e}")
                    notificar(f"❌ Error procesando {item['name']}: {str(e)[:100]}")
                finally:
                    if os.path.exists(temp_apk):
                        os.remove(temp_apk)
        else:
            print("ℹ️  No hay APKs nuevas para procesar")
        
        # Generar sistema
        generar_sistema_completo(sheet)
        print("✅ Web V37 Generada correctamente")
        
    except Exception as e:
        print(f"[ERROR FATAL] {e}")
        notificar(f"🚨 <b>Error crítico:</b> {str(e)[:200]}")
        raise

if __name__ == "__main__":
    main()
