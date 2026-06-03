import os
import json
import io
import time
import hashlib
import dropbox
import gspread
import re
import requests
from datetime import datetime
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
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      data={"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"})
    except: pass

def calcular_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(4096), b""): sha256.update(block)
    return sha256.hexdigest()

def nombre_seguro(texto):
    return re.sub(r'[^a-zA-Z0-9]', '_', str(texto).strip().lower())

# ---------------------------------------------------------
# LIMPIEZA DE DRIVE Y SHEETS (ELIMINAR VERSIONES VIEJAS)
# ---------------------------------------------------------
def eliminar_version_anterior(sheet, drive_service, dbx, pkg_nuevo, id_nuevo, nombre_app, version_nueva):
    """Elimina versiones anteriores de la misma app en Drive, Sheets y Dropbox"""
    try:
        # Recargar registros para tener datos frescos
        registros = sheet.get_all_records()
        filas_a_borrar = []
        
        for i, r in enumerate(registros):
            pkg_viejo = str(r.get('Pkg', '')).strip().lower()
            id_viejo = str(r.get('ID Drive', '')).strip()
            nombre_viejo = str(r.get('Nombre', '')).strip()
            version_vieja = str(r.get('Version', '')).strip()
            
            # Si es el mismo package pero diferente ID (versión diferente)
            if pkg_viejo == pkg_nuevo.lower() and id_viejo != id_nuevo:
                print(f"🗑️ Eliminando versión antigua: {nombre_viejo} v{version_vieja}")
                
                # 1. Eliminar de Google Drive
                try:
                    drive_service.files().delete(fileId=id_viejo).execute()
                    print(f"   ✅ Eliminado de Drive: {id_viejo}")
                except Exception as e:
                    print(f"   ⚠️  No se pudo borrar de Drive: {e}")
                
                # 2. Eliminar de Dropbox
                try:
                    ruta_dropbox = f"/{nombre_viejo}_{version_vieja}.apk"
                    dbx.files_delete_v2(ruta_dropbox)
                    print(f"   ✅ Eliminado de Dropbox: {ruta_dropbox}")
                except Exception as e:
                    print(f"   ⚠️  No se pudo borrar de Dropbox: {e}")
                
                # Marcar fila para borrar (después de procesar todo)
                filas_a_borrar.append(i + 2)  # +2 porque filas empiezan en 1 y hay header
        
        # 3. Eliminar filas de Sheets (de abajo hacia arriba para no desordenar índices)
        if filas_a_borrar:
            time.sleep(2)  # Esperar a que Google procese
            for fila_num in sorted(filas_a_borrar, reverse=True):
                try:
                    sheet.delete_row(fila_num)
                    print(f"   ✅ Fila {fila_num} eliminada de Sheets")
                    time.sleep(1.5)  # Rate limiting
                except Exception as e:
                    print(f"   ⚠️  Error borrando fila {fila_num}: {e}")
                    
    except Exception as e:
        print(f"[ERROR] En eliminación de versión anterior: {e}")

# ---------------------------------------------------------
# SINCRONIZACIÓN DE DROPBOX (ELIMINAR HUÉRFANOS)
# ---------------------------------------------------------
def sincronizar_dropbox(sheet, dbx):
    """Elimina archivos de Dropbox que no están en Sheets"""
    print("🧹 Escaneando Dropbox para purgar archivos huérfanos...")
    try:
        registros = sheet.get_all_records()
        
        # 1. Armamos la lista de archivos que SÍ deben existir
        archivos_legales = []
        for r in registros:
            if not r.get('Pkg'): continue
            nombre = str(r.get('Nombre')).strip()
            version = str(r.get('Version')).strip()
            ruta_esperada = f"/{nombre}_{version}.apk".lower()
            archivos_legales.append(ruta_esperada)
        
        # 2. Revisamos todo lo que hay en Dropbox
        resultado = dbx.files_list_folder('')
        for entrada in resultado.entries:
            if isinstance(entrada, dropbox.files.FileMetadata):
                # Si el archivo NO está en la lista legal, se elimina
                if entrada.path_display.lower() not in archivos_legales:
                    print(f"🗑️ Eliminando huérfano de Dropbox: {entrada.path_display}")
                    try:
                        dbx.files_delete_v2(entrada.path_display)
                    except Exception as e:
                        print(f"   No se pudo borrar {entrada.path_display}: {e}")
                         
    except Exception as e:
        print(f"Error en sincronización de Dropbox: {e}")

# ---------------------------------------------------------
# GENERADOR HTML Y JSON
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("🔄 Generando Sistema completo...")
    registros = sheet.get_all_records()
    
    obtainium_apps = []
    store_apps = []  # ← PARA TU APP ANDROID
    archivos_html_validos = ["index.html"]

    for r in registros:
        if not r.get('Pkg'): continue 
        
        nombre = str(r.get('Nombre', 'App')).strip()
        version = str(r.get('Version', '1.0')).strip()
        link_apk = str(r.get('Link APK', '')).strip().replace("dl=0", "dl=1")
        pkg = str(r.get('Pkg', '')).strip()
        icono = str(r.get('Icono', '')).strip()
        version_code = str(r.get('Version Code', '0')).strip()
        
        filename = f"{nombre_seguro(nombre)}.html"
        archivos_html_validos.append(filename)
        
        # HTML VÁLIDO (no Markdown)
        html_content = f"""<!DOCTYPE html>
<html>
<head><title>{nombre}</title></head>
<body>
<h1>{nombre}</h1>
<p>Version: {version}</p>
<a href="{link_apk}">Descargar {nombre} v{version}</a>
</body>
</html>"""
        
        with open(filename, "w", encoding='utf-8') as f: 
            f.write(html_content)
        
        # Obtainium JSON
        app_entry = {
            "id": pkg,
            "url": link_apk,
            "name": nombre,
            "version": version,
            "pinned": False,
            "categories": [],
            "preferredApkPath": "",
            "additionalSettings": ""
        }
        obtainium_apps.append(app_entry)
        
        # Store JSON (para tu app Android)
        store_apps.append({
            "pkg": pkg,
            "name": nombre,
            "versionName": version,
            "versionCode": int(version_code) if version_code.isdigit() else 0,
            "apkUrl": link_apk,
            "icon": icono if icono else f"{REPO_URL_BASE}default_icon.png"
        })

    # Limpieza de HTMLs viejos
    try:
        archivos_locales = os.listdir('.')
        for archivo in archivos_locales:
            if archivo.endswith('.html') and archivo not in archivos_html_validos:
                os.remove(archivo)
                print(f"🗑️ HTML huérfano eliminado: {archivo}")
    except Exception as e:
        print(f"Error al limpiar HTMLs: {e}")

    # Guardar JSONs
    export_data = {
        "debug": "GENERADO_POR_BOT_CONFIRMADO", 
        "apps": obtainium_apps
    }
    with open("obtainium.json", "w", encoding='utf-8') as f: 
        json.dump(export_data, f, indent=4)
    
    # ← GUARDAR store.json
    with open("store.json", "w", encoding='utf-8') as f: 
        json.dump(store_apps, f, indent=2, ensure_ascii=False)
    
    print(f"✅ JSONs generados: {len(store_apps)} apps")
    
    with open("index.html", "w", encoding='utf-8') as f: 
        f.write(f"<html><body><h1>V38 Online - Tienda APK</h1><p>Apps: {len(store_apps)}</p></body></html>")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor con Limpieza Completa...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    try:
        # Recargar sheet para tener datos frescos
        sheet = client_gs.open_by_key(SHEET_ID).sheet1
        registros = sheet.get_all_records()
        procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
        
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

        if nuevos:
            notificar(f"👷‍♂️ Procesando {len(nuevos)} APKs")
            
            for item in nuevos:
                temp_apk = "temp.apk"
                try:
                    # Descargar APK
                    request = drive_service.files().get_media(fileId=item['id'])
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    fh.seek(0)
                    with open(temp_apk, "wb") as f: f.write(fh.read())

                    # Parsear APK
                    apk = APK(temp_apk)
                    nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                    
                    link_icon = f"{REPO_URL_BASE}default_icon.png"

                    # Subir a Dropbox
                    path = f"/{nombre}_{apk.version_name}.apk"
                    with open(temp_apk, "rb") as f: dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("dl=0", "dl=1")

                    # Agregar a Sheets
                    sheet.append_row([
                        nombre, "Publicado", link_apk, apk.version_name, 
                        apk.package, link_icon, item['id'], "Dropbox", 
                        str(apk.version_code), calcular_hash(temp_apk), 
                        str(os.path.getsize(temp_apk))
                    ])
                    
                    print(f"✅ Agregado a Sheets: {nombre} v{apk.version_name}")
                    
                    # ← LIMPIEZA COMPLETA: Eliminar versión anterior
                    eliminar_version_anterior(
                        sheet, drive_service, dbx, 
                        apk.package, item['id'], 
                        nombre, apk.version_name
                    )
                    
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                    time.sleep(2)  # Rate limiting entre APKs
                    
                except Exception as e: 
                    print(f"[ERROR] Procesando {item['name']}: {e}")
                    notificar(f"❌ Error: {str(e)[:100]}")
                finally: 
                    if os.path.exists(temp_apk): os.remove(temp_apk)
        else:
            print("ℹ️  No hay APKs nuevas para procesar")
            
    except Exception as e: 
        print(f"[ERROR] En procesamiento: {e}")
        notificar(f"🚨 Error: {str(e)[:100]}")

    # ← 1. SINCRONIZAR DROPBOX (eliminar huérfanos)
    sincronizar_dropbox(sheet, dbx)
    
    # ← 2. GENERAR SITIO WEB Y JSONS
    generar_sistema_completo(sheet)
    
    print("✅ Web Generada y Almacenamiento Optimizado.")

if __name__ == "__main__":
    main()
