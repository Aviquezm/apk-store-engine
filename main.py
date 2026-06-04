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
# 🆕 AGREGADO: RECONCILIACIÓN DRIVE ↔ SHEETS (sin tocar nada más)
# ---------------------------------------------------------
def reconciliar_todo(sheet, drive_service, dbx):
    """Compara Drive, Sheets y Dropbox. Elimina lo que falte en Drive."""
    print("🔄 INICIANDO RECONCILIACIÓN COMPLETA...")
    try:
        # 1. Obtener TODOS los archivos APK en Drive
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items_drive = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        ids_en_drive = {item['id'] for item in items_drive if item['name'].lower().endswith('.apk')}
        
        print(f"📁 Archivos en Drive: {len(ids_en_drive)}")
        
        # 2. Obtener TODOS los registros en Sheets
        registros = sheet.get_all_records()
        print(f"📊 Registros en Sheets: {len(registros)}")
        
        # 3. Detectar qué IDs en Sheets YA NO ESTÁN en Drive (fueron eliminados)
        filas_a_eliminar = []
        for idx, r in enumerate(registros):
            id_drive = str(r.get('ID Drive', '')).strip()
            nombre = str(r.get('Nombre', '')).strip()
            version = str(r.get('Version', '')).strip()
            
            if id_drive and id_drive not in ids_en_drive:
                print(f"🗑️ DETECTADO: {nombre} v{version} fue ELIMINADO de Drive")
                filas_a_eliminar.append((idx + 2, nombre, version, id_drive))
        
        # 4. Eliminar de Dropbox y Sheets lo que ya no está en Drive
        if filas_a_eliminar:
            print(f"🧹 Limpiando {len(filas_a_eliminar)} archivos eliminados...")
            
            for fila_num, nombre, version, id_drive in sorted(filas_a_eliminar, key=lambda x: x[0], reverse=True):
                
                # a) Eliminar de Dropbox
                ruta_dropbox = f"/{nombre}_{version}.apk"
                try:
                    dbx.files_delete_v2(ruta_dropbox)
                    print(f"   ✅ Dropbox: {ruta_dropbox} eliminado")
                except Exception as e:
                    print(f"   ⚠️ No se pudo borrar de Dropbox: {e}")
                
                # b) Eliminar fila de Sheets
                try:
                    sheet.delete_row(fila_num)
                    print(f"   ✅ Sheets: Fila {fila_num} eliminada")
                    time.sleep(1.5)
                except Exception as e:
                    print(f"   ⚠️ Error eliminando fila {fila_num}: {e}")
        else:
            print("✅ No hay archivos eliminados que limpiar")
            
    except Exception as e:
        print(f"[ERROR] En reconciliación: {e}")
        import traceback
        traceback.print_exc()

# ---------------------------------------------------------
# 🆕 AGREGADO: DETECTAR CAMBIOS DE NOMBRE
# ---------------------------------------------------------
def detectar_cambios_nombre(sheet, drive_service):
    """Detecta si el nombre de un archivo en Drive cambió respecto a Sheets"""
    print("🔍 Detectando cambios de nombre...")
    try:
        # 1. Obtener todos los APKs en Drive
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items_drive = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        items_apk = [i for i in items_drive if i['name'].lower().endswith('.apk')]
        
        # 2. Obtener registros de Sheets
        registros = sheet.get_all_records()
        
        cambios_detectados = 0
        
        for item in items_apk:
            item_id = str(item['id']).strip()
            item_nombre_archivo = item['name']
            
            # Buscar en Sheets
            for r in registros:
                id_sheet = str(r.get('ID Drive', '')).strip()
                nombre_sheet = str(r.get('Nombre', '')).strip()
                version_sheet = str(r.get('Version', '')).strip()
                
                # Si el ID coincide
                if id_sheet == item_id:
                    # El nombre esperado en Dropbox es: {nombre}_{version}.apk
                    nombre_esperado_dropbox = f"{nombre_sheet}_{version_sheet}.apk"
                    
                    # Si el nombre del archivo en Drive es diferente al esperado
                    if item_nombre_archivo != nombre_esperado_dropbox:
                        print(f"🔄 DETECTADO: Archivo cambió de '{nombre_esperado_dropbox}' a '{item_nombre_archivo}'")
                        
                        # Actualizar el nombre en Sheets (Columna 1 = Nombre)
                        try:
                            fila_num = registros.index(r) + 2 
                            sheet.update_cell(fila_num, 1, item_nombre_archivo)
                            print(f"   ✅ Nombre actualizado en Sheets")
                            cambios_detectados += 1
                        except Exception as e:
                            print(f"   ⚠️ Error actualizando Sheets: {e}")
                    break
        
        if cambios_detectados == 0:
            print("✅ No hay cambios de nombre detectados")
        else:
            print(f"✅ {cambios_detectados} cambio(s) de nombre detectado(s)")
            
    except Exception as e:
        print(f"[ERROR] Detectando cambios de nombre: {e}")

# ---------------------------------------------------------
# SINCRONIZACIÓN ABSOLUTA DE DROPBOX (EL BARRENDERO)
# ---------------------------------------------------------
def sincronizar_dropbox(sheet, dbx):
    print("🧹 Escaneando Dropbox para purgar archivos huérfanos...")
    try:
        registros = sheet.get_all_records()
        
        # 1. Armamos la lista de los archivos que SÍ deben existir
        archivos_legales = []
        for r in registros:
            if not r.get('Pkg'): continue
            nombre = str(r.get('Nombre')).strip()
            version = str(r.get('Version')).strip()
            ruta_esperada = f"/{nombre}_{version}.apk".lower()
            archivos_legales.append(ruta_esperada)
        
        # 2. Revisamos todo lo que hay realmente en Dropbox
        resultado = dbx.files_list_folder('')
        for entrada in resultado.entries:
            if isinstance(entrada, dropbox.files.FileMetadata):
                # Si el archivo de Dropbox no está en el Excel, se elimina
                if entrada.path_display.lower() not in archivos_legales:
                    print(f"🗑️ Eliminando basura de Dropbox: {entrada.path_display}")
                    try:
                        dbx.files_delete_v2(entrada.path_display)
                    except Exception as e:
                        print(f"No se pudo borrar {entrada.path_display}: {e}")
                         
    except Exception as e:
        print(f"Error en la sincronización de Dropbox: {e}")

# ---------------------------------------------------------
# GENERADOR HTML Y JSON (CON LIMPIEZA DE HUÉRFANOS)
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("🔄 Generando Sistema y limpiando HTMLs...")
    registros = sheet.get_all_records()
    
    obtainium_apps = []
    archivos_html_validos = ["index.html"]

    for r in registros:
        if not r.get('Pkg'): continue 
        
        nombre = str(r.get('Nombre', 'App')).strip()
        version = str(r.get('Version', '1.0')).strip()
        link_apk = str(r.get('Link APK', '')).strip().replace("dl=0", "dl=1")
        pkg = str(r.get('Pkg', '')).strip()
        
        filename = f"{nombre_seguro(nombre)}.html"
        archivos_html_validos.append(filename)
        
        full_url = f"{REPO_URL_BASE}{filename}"
        
        html_content = f"""
        
{nombre}
Version: {version}
[Descargar {nombre} v{version}]({link_apk})
        """
        with open(filename, "w", encoding='utf-8') as f: f.write(html_content)
        
        # --- AQUÍ ESTÁ LA MAGIA DE LA NUEVA TIENDA NATIVA ---
        app_entry = {
            "id": pkg,
            "url": link_apk,  # Descarga directa del APK desde Dropbox
            "name": nombre,
            "version": version,
            "pinned": False,
            "categories": [],
            "preferredApkPath": "",
            "additionalSettings": "" # Limpiamos el flag de Obtainium
        }
        obtainium_apps.append(app_entry)

    # Limpieza de HTMLs viejos en GitHub
    try:
        archivos_locales = os.listdir('.')
        for archivo in archivos_locales:
            if archivo.endswith('.html') and archivo not in archivos_html_validos:
                os.remove(archivo)
                print(f"🗑️ HTML huérfano eliminado: {archivo}")
    except Exception as e:
        print(f"Error al limpiar HTMLs: {e}")

    export_data = {
        "debug": "GENERADO_POR_BOT_CONFIRMADO", 
        "apps": obtainium_apps
    }
    with open("obtainium.json", "w", encoding='utf-8') as f: json.dump(export_data, f, indent=4)
    with open("index.html", "w", encoding='utf-8') as f: f.write("<html><body><h1>Motor Online (Espejo Activo)</h1></body></html>")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor (Modo Alta Velocidad)...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    # 🆕 PASO NUEVO 0: RECONCILIACIÓN (detectar eliminados manualmente de Drive)
    reconciliar_todo(sheet, drive_service, dbx)
    
    # 🆕 PASO NUEVO 0.5: DETECTAR CAMBIOS DE NOMBRE
    sheet = client_gs.open_by_key(SHEET_ID).sheet1  # Recargar después de eliminar filas
    detectar_cambios_nombre(sheet, drive_service)
    
    # 🆕 Recargar sheet antes de procesar nuevas APKs
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    try:
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
                    request = drive_service.files().get_media(fileId=item['id'])
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    fh.seek(0)
                    with open(temp_apk, "wb") as f: f.write(fh.read())

                    apk = APK(temp_apk)
                    nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                    
                    # --- NUEVO NOMBRE DEL ICONO ESTÁTICO ---
                    link_icon = f"{REPO_URL_BASE}default_icon.png"

                    path = f"/{nombre}_{apk.version_name}.apk"
                    with open(temp_apk, "rb") as f: dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("dl=0", "dl=1")

                    sheet.append_row([nombre, "Publicado", link_apk, apk.version_name, apk.package, link_icon, item['id'], "Dropbox", str(apk.version_code), calcular_hash(temp_apk), str(os.path.getsize(temp_apk))])
                    
                    try:
                        filas_a_borrar = []
                        pkg_nuevo = str(apk.package).strip().lower()
                        for i, r in enumerate(registros):
                            pkg_viejo = str(r.get('Pkg')).strip().lower()
                            id_viejo = str(r.get('ID Drive', '')).strip()
                            if pkg_viejo == pkg_nuevo and id_viejo != item['id']:
                                try: drive_service.files().delete(fileId=id_viejo).execute()
                                except: pass
                                filas_a_borrar.append(i + 2)
                        if filas_a_borrar:
                            for fila_num in sorted(filas_a_borrar, reverse=True):
                                sheet.delete_row(fila_num)
                                time.sleep(1.5)
                    except: pass
                    
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                except Exception as e: print(e)
                finally: 
                    if os.path.exists(temp_apk): os.remove(temp_apk)
    except Exception as e: print(e)

    # 1. Pasamos la escoba por Dropbox
    sincronizar_dropbox(sheet, dbx)
    
    # 2. Generamos el sitio y limpiamos GitHub
    generar_sistema_completo(sheet)
    
    print("✅ Web Generada y Almacenamiento Optimizado.")

if __name__ == "__main__":
    main()
