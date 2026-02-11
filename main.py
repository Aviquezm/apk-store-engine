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
REPO_URL = os.environ['REPO_URL']

DBX_KEY = os.environ['DROPBOX_APP_KEY']
DBX_SECRET = os.environ['DROPBOX_APP_SECRET']
DBX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') 
TG_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# ---------------------------------------------------------
# 1. UTILIDADES
# ---------------------------------------------------------
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        requests.post(url, data=data)
    except Exception as e: print(f"⚠️ Telegram Error: {e}")

def limpiar_texto(texto):
    if not texto: return ""
    return str(texto).strip().lower().replace('\n', '').replace('\r', '').replace('\t', '')

def calcular_hash(file_path):
    """Calcula el SHA256 para seguridad"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

# ---------------------------------------------------------
# 2. MOTOR DE ICONOS V22 (Invencible)
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    candidatos_fb = [] 
    
    print(f"\n🕵️‍♂️ [Autopsia] Buscando icono para: {app_name}")
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            for nombre in z.namelist():
                nombre_lc = nombre.lower()
                if not (nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre): continue
                if 'notification' in nombre_lc or 'abc_' in nombre_lc or 'splash' in nombre_lc: continue 

                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    if abs(w - h) > 2 or w < 48: continue 
                    
                    puntuacion = 0
                    if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 10000
                    if 'ic_launcher_round' in nombre_lc: puntuacion += 5000
                    if 'ic_launcher' in nombre_lc: puntuacion += 4500
                    if 'app_icon' in nombre_lc: puntuacion += 4000
                    
                    prioridad_fb = 0
                    if 96 <= w <= 192: prioridad_fb = 3 
                    elif 193 <= w <= 512: prioridad_fb = 2
                    elif w > 512: prioridad_fb = 1
                    candidatos_fb.append((nombre, prioridad_fb, w, data))

                    if puntuacion > 0:
                        if puntuacion > mejor_puntuacion:
                            mejor_puntuacion = puntuacion
                            mejor_data = data
                            
                except: continue
            
            if mejor_puntuacion > 1000: return mejor_data
            
            if candidatos_fb:
                candidatos_fb.sort(key=lambda x: (x[1], x[2]), reverse=True)
                return candidatos_fb[0][3]
            return None
    except: return None

# ---------------------------------------------------------
# 3. LIMPIEZA
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        pkg_nuevo = limpiar_texto(pkg_nuevo_raw)
        
        print(f"\n🔍 [Limpieza] Buscando rastros de: '{pkg_nuevo}'...")
        for i, r in enumerate(registros):
            pkg_viejo = limpiar_texto(r.get('Pkg'))
            id_viejo = str(r.get('ID Drive', '')).strip()
            
            if pkg_viejo == pkg_nuevo and id_viejo != id_archivo_nuevo:
                print(f"   🚨 DUPLICADO (Fila {i+2})")
                try: drive_service.files().delete(fileId=id_viejo).execute()
                except: pass
                try: 
                    nombre_dbx = f"/{r.get('Nombre', '').replace(' ', '_')}_v{r.get('Version', '')}.apk"
                    dbx.files_delete_v2(nombre_dbx)
                except: pass
                filas_a_borrar.append(i + 2)
                archivos_borrados += 1

        if filas_a_borrar:
            for fila_num in sorted(filas_a_borrar, reverse=True):
                sheet.delete_row(fila_num)
                time.sleep(1.5)
        return archivos_borrados
    except Exception as e:
        print(f"❌ Error Limpieza: {e}")
        return 0

# ---------------------------------------------------------
# 4. GENERADOR WEB (OPTIMIZADO PARA OBTAINIUM)
# ---------------------------------------------------------
def generar_archivos_finales(sheet):
    print("🔄 Generando Web 'index.html' para Obtainium...")
    registros = sheet.get_all_records()
    
    # HTML Oscuro y Limpio
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Tienda APK Privada</title>
        <style>
            body { background-color: #121212; color: #ffffff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; padding: 20px; }
            h1 { text-align: center; color: #00e676; }
            .container { max-width: 800px; margin: 0 auto; }
            .app-card { background-color: #1e1e1e; border-radius: 12px; padding: 15px; margin-bottom: 15px; display: flex; align-items: center; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
            .app-icon { width: 64px; height: 64px; border-radius: 12px; margin-right: 20px; object-fit: cover; }
            .app-info { flex-grow: 1; }
            .app-name { font-size: 1.2em; font-weight: bold; margin: 0; }
            .app-version { color: #aaaaaa; font-size: 0.9em; margin: 5px 0; }
            .btn-download { background-color: #00e676; color: #000000; text-decoration: none; padding: 8px 16px; border-radius: 20px; font-weight: bold; font-size: 0.9em; display: inline-block; }
            .btn-download:hover { background-color: #00c853; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📦 Mis Aplicaciones</h1>
    """

    for r in registros:
        if not r.get('Pkg'): continue
        
        nombre = r.get('Nombre', 'App Desconocida')
        version = r.get('Version', '1.0')
        link_apk = r.get('Link APK', '#')
        link_icon = r.get('Link Icono', 'https://via.placeholder.com/64')
        pkg = r.get('Pkg', '')

        # Bloque HTML por cada App
        html += f"""
            <div class="app-card" id="{pkg}">
                <img src="{link_icon}" alt="Icono" class="app-icon">
                <div class="app-info">
                    <h2 class="app-name">{nombre}</h2>
                    <p class="app-version">v{version}</p>
                    <a href="{link_apk}" class="btn-download">Descargar APK</a>
                </div>
            </div>
        """
    
    html += """
        </div>
        <p style="text-align: center; color: #666; margin-top: 30px;">Generado automáticamente por Bot V25</p>
    </body>
    </html>
    """
    
    # Guardar index.html (Para Obtainium)
    with open("index.html", "w", encoding='utf-8') as f: f.write(html)
    
    # Guardar index.json (Copia de seguridad / Compatibilidad)
    repo_data = {"repo": {"name": "Mi Tienda", "version": 1}, "apps": registros}
    with open("index.json", "w", encoding='utf-8') as f: json.dump(repo_data, f, indent=4)

# ---------------------------------------------------------
# 5. MAIN
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
    except:
        links = dbx.sharing_list_shared_links(path=dest_path, direct_only=True).links
        url = links[0].url if links else None
    return url.replace("?dl=0", "?dl=1") if url else None

def main():
    print("🚀 Iniciando Motor V25 (Completo)...")
    
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    
    nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

    if nuevos:
        notificar(f"👷‍♂️ <b>Procesando {len(nuevos)} APKs</b>")
        for item in nuevos:
            file_id = str(item['id']).strip()
            file_name = item['name']
            print(f"\n⚙️ Procesando: {file_name}")
            
            temp_apk = "temp.apk"
            try:
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # ANÁLISIS
                apk = APK(temp_apk)
                nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                
                # 1. Icono
                icon_data = extraer_icono_precision(temp_apk, apk.application)
                icon_filename = f"icon_{apk.package}.png"
                
                # 2. Hash & Size
                apk_hash = calcular_hash(temp_apk)
                apk_size = os.path.getsize(temp_apk)
                
                # Subidas
                nombre_final = f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk"
                link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
                
                link_icon = "https://via.placeholder.com/150" 
                if icon_data:
                    with open(icon_filename, "wb") as f: f.write(icon_data)
                    url_subida = subir_a_dropbox(dbx, icon_filename, icon_filename)
                    if url_subida: link_icon = url_subida
                    os.remove(icon_filename)

                # Guardar en Excel
                sheet.append_row([
                    nombre_limpio, "Publicado", link_apk, apk.version_name, 
                    apk.package, link_icon, file_id, "Dropbox", 
                    str(apk.version_code), apk_hash, str(apk_size)
                ])
                
                eliminar_rastros_anteriores(sheet, drive_service, dbx, apk.package, file_id)
                notificar(f"✅ <b>{nombre_limpio}</b> v{apk.version_name} listo.")

            except Exception as e:
                notificar(f"❌ Error con {file_name}: {e}")
                print(f"❌ Error: {e}")
            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)
    else:
        print("💤 Sin novedades en Drive.")

    # SIEMPRE regenerar la web al final
    generar_archivos_finales(sheet)
    print("✅ Página Web actualizada.")

if __name__ == "__main__":
    main()
