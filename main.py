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

# ---------------------------------------------------------
# EXTRACCIÓN DE ICONOS - VERSIÓN ORIGINAL RESTAURADA
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    """Extrae el mejor icono del APK buscando en todas las resoluciones"""
    mejor_puntuacion = -1
    mejor_data = None
    mejor_nombre = None
    
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            # Lista todos los archivos del APK
            for nombre_archivo in z.namelist():
                # Solo buscamos PNGs o WebP en carpetas res/
                if not (nombre_archivo.lower().endswith(('.png', '.webp')) and 'res/' in nombre_archivo):
                    continue
                
                # Saltamos iconos de notificación
                if 'notification' in nombre_archivo.lower():
                    continue
                
                try:
                    # Leemos la imagen
                    data = z.read(nombre_archivo)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    
                    # Skip imágenes muy pequeñas
                    if w < 48 or h < 48:
                        continue
                    
                    # Sistema de puntuación
                    puntuacion = 0
                    
                    # Prioridad máxima si es ic_launcher
                    if 'ic_launcher' in nombre_archivo.lower():
                        puntuacion += 1000
                    
                    # Bonus por resolución (mientras más grande, mejor)
                    if w >= 192:
                        puntuacion += 300  # xxxhdpi
                    elif w >= 144:
                        puntuacion += 200  # xxhdpi
                    elif w >= 96:
                        puntuacion += 100  # xhdpi
                    elif w >= 72:
                        puntuacion += 50   # hdpi
                    
                    # Bonus si está en mipmap (generalmente son los launchers)
                    if 'mipmap' in nombre_archivo.lower():
                        puntuacion += 50
                    
                    # Si es mejor que el anterior, lo guardamos
                    if puntuacion > mejor_puntuacion:
                        mejor_puntuacion = puntuacion
                        mejor_data = data
                        mejor_nombre = nombre_archivo
                        
                except Exception as e:
                    # Si falla una imagen, continuamos con la siguiente
                    continue
        
        if mejor_data:
            print(f"✅ Icono encontrado: {mejor_nombre} ({mejor_puntuacion} pts)")
        
        return mejor_data
        
    except Exception as e:
        print(f"[ERROR] Extrayendo icono: {e}")
        return None

# ---------------------------------------------------------
# LIMPIEZA - VERSIÓN ORIGINAL
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
                    nombre_viejo = r.get('Nombre', '').replace(' ', '_')
                    version_vieja = r.get('Version', '')
                    dbx.files_delete_v2(f"/{nombre_viejo}_v{version_vieja}.apk")
                except Exception as e:
                    print(f"[WARN] No se pudo borrar de Dropbox: {e}")
                
                filas_a_borrar.append(i + 2)  # +2 porque las filas empiezan en 1 y hay header
        
        # Borramos de abajo hacia arriba para no desordenar índices
        if filas_a_borrar:
            for fila_num in sorted(filas_a_borrar, reverse=True):
                try:
                    sheet.delete_row(fila_num)
                    time.sleep(1.5)  # Rate limiting
                except Exception as e:
                    print(f"[WARN] Error borrando fila {fila_num}: {e}")
                    
    except Exception as e:
        print(f"[ERROR] En limpieza: {e}")

# ---------------------------------------------------------
# GENERADOR V38 RESTAURADO
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("🔄 Generando Sistema V38 Restaurado...")
    registros = sheet.get_all_records()
    
    obtainium_apps = []
    store_apps = []

    for r in registros:
        if not r.get('Pkg'):
            continue
        
        # Blindaje de datos
        nombre = str(r.get('Nombre', 'App')).strip()
        version = str(r.get('Version', '1.0')).strip()
        link_apk = str(r.get('Link APK', '')).strip()
        pkg = str(r.get('Pkg', '')).strip()
        icono = str(r.get('Icono', '')).strip()
        version_code = str(r.get('Version Code', '0')).strip()
        
        # HTML Individual
        filename = f"{nombre_seguro(nombre)}.html"
        full_url = f"{REPO_URL_BASE}{filename}"
        
        html_content = f"""<!DOCTYPE html>
<html>
<head><title>{nombre}</title></head>
<body>
<h1>{nombre}</h1>
<p>Version: {version}</p>
<a href="{link_apk}">Descargar APK</a>
</body>
</html>"""
        
        with open(filename, "w", encoding='utf-8') as f:
            f.write(html_content)
        
        # Obtainium JSON
        settings_dict = {
            "customLinkFilterRegex": "\\.apk",
            "filterByLinkText": False,
            "versionExtractWholePage": True,
            "versionExtractionRegEx": "Version:\\s*([0-9a-zA-Z\\.\\-]+)",
            "matchGroupToUse": "$1",
            "versionDetection": True,
            "apkFilterRegEx": "",
            "invertAPKFilter": False,
            "autoApkFilterByArch": False,
            "appName": nombre,
            "allowInsecure": False,
            "refreshBeforeDownload": False
        }
        
        app_entry = {
            "id": pkg,
            "url": full_url,
            "author": pkg,
            "name": nombre,
            "additionalSettings": json.dumps(settings_dict, ensure_ascii=False),
            "pinned": False,
            "categories": [],
            "overrideSource": "HTML"
        }
        obtainium_apps.append(app_entry)
        
        # Store JSON (para tu app Android)
        store_apps.append({
            "pkg": pkg,
            "name": nombre,
            "versionName": version,
            "versionCode": int(version_code) if version_code.isdigit() else 0,
            "apkUrl": link_apk,
            "icon": icono if icono else "https://via.placeholder.com/64"
        })

    # Guardar JSONs
    with open("obtainium.json", "w", encoding='utf-8') as f:
        json.dump(obtainium_apps, f, indent=2, ensure_ascii=False)
    
    with open("store.json", "w", encoding='utf-8') as f:
        json.dump(store_apps, f, indent=2, ensure_ascii=False)
    
    print(f"✅ JSONs generados: {len(store_apps)} apps")
    
    with open("index.html", "w", encoding='utf-8') as f:
        f.write(f"<html><body><h1>V38 Restaurado - Tienda APK</h1><p>Apps: {len(store_apps)}</p></body></html>")

# ---------------------------------------------------------
# MAIN - VERSIÓN RESTAURADA CON FIX DE SHEETS
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor V38 Restaurado...")
    
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
        
        # Obtener registros existentes
        registros = sheet.get_all_records()
        procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
        
        # Buscar nuevos archivos en Drive
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
                    print(f"📥 Descargando {item['name']}...")
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
                    print(f"🔍 Analizando {item['name']}...")
                    apk = APK(temp_apk)
                    
                    # Nombre limpio (sin versión)
                    nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                    if not nombre:
                        nombre = apk.package.split('.')[-1]
                    
                    # Extraer icono
                    print(f"🎨 Extrayendo icono...")
                    icon_data = extraer_icono_precision(temp_apk, apk.application)
                    
                    link_icon = "https://via.placeholder.com/64"
                    if icon_data:
                        try:
                            # Subir icono a Dropbox
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
                            print(f"✅ Icono subido a Dropbox")
                            
                        except Exception as e:
                            print(f"[WARN] Error subiendo icono: {e}")

                    # Subir APK a Dropbox
                    print(f"📤 Subiendo APK a Dropbox...")
                    path = f"/{nombre}_{apk.version_name}.apk"
                    with open(temp_apk, "rb") as f:
                        dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("?dl=0", "?dl=1")
                    
                    # AGREGAR FILA NUEVA (NO REEMPLAZAR)
                    print(f"📝 Agregando a Google Sheets...")
                    sheet.append_row([
                        nombre,                    # Columna A: Nombre
                        "Publicado",               # Columna B: Estado
                        link_apk,                  # Columna C: Link APK
                        apk.version_name,          # Columna D: Version
                        apk.package,               # Columna E: Pkg
                        link_icon,                 # Columna F: Icono
                        item['id'],                # Columna G: ID Drive
                        "Dropbox",                 # Columna H: CDN
                        str(apk.version_code),     # Columna I: Version Code
                        calcular_hash(temp_apk),   # Columna J: Hash
                        str(os.path.getsize(temp_apk))  # Columna K: Tamaño
                    ])
                    print(f"✅ Agregado a Sheets correctamente")
                    
                    # Limpiar versiones anteriores
                    eliminar_rastros_anteriores(sheet, drive_service, dbx, apk.package, item['id'])
                    
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                    time.sleep(1)
                    
                except Exception as e:
                    print(f"[ERROR] Procesando {item['name']}: {e}")
                    import traceback
                    traceback.print_exc()
                    notificar(f"❌ Error: {str(e)[:100]}")
                finally:
                    if os.path.exists(temp_apk):
                        os.remove(temp_apk)
        else:
            print("ℹ️  No hay APKs nuevas para procesar")
        
        # Generar sistema
        generar_sistema_completo(sheet)
        print("✅ Web V38 Generada correctamente")
        
    except Exception as e:
        print(f"[ERROR FATAL] {e}")
        import traceback
        traceback.print_exc()
        notificar(f"🚨 <b>Error crítico:</b> {str(e)[:200]}")
        raise

if __name__ == "__main__":
    main()
