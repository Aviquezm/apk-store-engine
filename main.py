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
# RECONCILIACIÓN COMPLETA: DRIVE ↔ SHEETS ↔ DROPBOX
# ---------------------------------------------------------
def reconciliar_todo(sheet, drive_service, dbx):
    """Compara Drive, Sheets y Dropbox. Elimina lo que falte en Drive."""
    print("🔄 INICIANDO RECONCILIACIÓN COMPLETA...")
    try:
        # 1. Obtener TODOS los archivos APK en Drive (CON PAGINACIÓN)
        ids_en_drive = set()
        page_token = None
        while True:
            query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
            response = drive_service.files().list(
                q=query, 
                pageSize=1000, 
                fields="nextPageToken, files(id, name)",
                pageToken=page_token
            ).execute()
            
            for item in response.get('files', []):
                if item['name'].lower().endswith('.apk'):
                    ids_en_drive.add(item['id'])
                    
            page_token = response.get('nextPageToken')
            if not page_token:
                break
                
        print(f"📁 Archivos APK válidos en Drive: {len(ids_en_drive)}")
        
        # 2. Obtener TODOS los registros en Sheets
        registros = sheet.get_all_records()
        print(f"📊 Registros leídos en Sheets: {len(registros)}")
        
        if registros:
            print(f"📝 Columnas detectadas: {list(registros[0].keys())}")
        
        # 3. Detectar qué IDs en Sheets YA NO ESTÁN en Drive
        filas_a_eliminar = []
        for idx, r in enumerate(registros):
            id_drive = str(r.get('ID Drive', '')).strip()
            nombre = str(r.get('Nombre', '')).strip()
            version = str(r.get('Version', '')).strip()
            pkg = str(r.get('Pkg', '')).strip()
            
            # Ignorar filas totalmente en blanco en el Excel
            if not nombre and not pkg:
                continue
                
            if id_drive:
                if id_drive not in ids_en_drive:
                    print(f"🗑️ DETECTADO: '{nombre}' v{version} fue ELIMINADO de Drive.")
                    filas_a_eliminar.append((nombre, version, id_drive))
            else:
                print(f"⚠️ Fila {idx + 2} ('{nombre}') no tiene 'ID Drive'. Se ignorará.")
        
        # 4. Eliminar de Dropbox y Sheets lo que ya no está en Drive
        if filas_a_eliminar:
            print(f"🧹 Limpiando {len(filas_a_eliminar)} archivos eliminados...")
            
            for nombre, version, id_drive in filas_a_eliminar:
                # a) Eliminar de Dropbox
                ruta_dropbox_esperada = f"/{nombre}_{version}.apk".lower()
                try:
                    resultado = dbx.files_list_folder('')
                    for entrada in resultado.entries:
                        if isinstance(entrada, dropbox.files.FileMetadata):
                            if entrada.path_lower == ruta_dropbox_esperada:
                                dbx.files_delete_v2(entrada.path_display)
                                print(f"   ✅ Dropbox: {entrada.path_display} eliminado")
                                break
                except Exception as e:
                    print(f"   ⚠️ No se pudo borrar de Dropbox: {e}")
                
                # b) Eliminar fila de Sheets (Buscador exacto)
                try:
                    celda = sheet.find(id_drive)
                    if celda:
                        sheet.delete_row(celda.row)
                        print(f"   ✅ Sheets: Fila de '{nombre}' eliminada correctamente")
                        time.sleep(1.5)
                except Exception as e:
                    print(f"   ⚠️ Error buscando/eliminando fila en Sheets: {e}")
        else:
            print("✅ Todo está sincronizado. No hay archivos eliminados que limpiar.")
            
    except Exception as e:
        print(f"[ERROR] En reconciliación: {e}")
        import traceback
        traceback.print_exc()

# ---------------------------------------------------------
# DETECTAR CAMBIOS DE NOMBRE
# ---------------------------------------------------------
def detectar_cambios_nombre(sheet, drive_service):
    print("🔍 Detectando cambios de nombre...")
    try:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items_drive = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        items_apk = [i for i in items_drive if i['name'].lower().endswith('.apk')]
        
        registros = sheet.get_all_records()
        cambios_detectados = 0
        
        for item in items_apk:
            item_id = str(item['id']).strip()
            item_nombre_archivo = item['name']
            
            for r in registros:
                id_sheet = str(r.get('ID Drive', '')).strip()
                nombre_sheet = str(r.get('Nombre', '')).strip()
                version_sheet = str(r.get('Version', '')).strip()
                
                if id_sheet == item_id:
                    nombre_esperado_dropbox = f"{nombre_sheet}_{version_sheet}.apk"
                    
                    if item_nombre_archivo != nombre_esperado_dropbox:
                        print(f"🔄 DETECTADO: Archivo cambió de '{nombre_esperado_dropbox}' a '{item_nombre_archivo}'")
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
# RESTAURACIÓN DE APKs FALTANTES EN DROPBOX
# ---------------------------------------------------------
def restaurar_faltantes_en_dropbox(sheet, drive_service, dbx):
    print("🔄 Verificando integridad de Dropbox vs Excel...")
    try:
        resultado = dbx.files_list_folder('')
        archivos_en_dropbox = [e.path_display.lower() for e in resultado.entries if isinstance(e, dropbox.files.FileMetadata)]
        
        registros = sheet.get_all_records()
        
        for r in registros:
            if not r.get('Pkg'): continue
            nombre = str(r.get('Nombre', '')).strip()
            version = str(r.get('Version', '')).strip()
            id_drive = str(r.get('ID Drive', '')).strip()
            
            ruta_esperada = f"/{nombre}_{version}.apk"
            
            if ruta_esperada.lower() not in archivos_en_dropbox and id_drive:
                print(f"⚠️ Faltante detectado: {ruta_esperada}. Restaurando desde Drive...")
                try:
                    request = drive_service.files().get_media(fileId=id_drive)
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done: _, done = downloader.next_chunk()
                    fh.seek(0)
                    with open("temp.apk", "wb") as f: f.write(fh.read())
                    
                    with open("temp.apk", "rb") as f: 
                        dbx.files_upload(f.read(), ruta_esperada, mode=WriteMode('overwrite'))
                    print(f"   ✅ Restaurado en Dropbox: {ruta_esperada}")
                except Exception as e:
                    print(f"   ⚠️ Error restaurando {nombre}: {e}")
                    
        print("✅ Verificación de integridad terminada.")
    except Exception as e:
        print(f"[ERROR] En restauración de faltantes: {e}")

# ---------------------------------------------------------
# SINCRONIZACIÓN DE DROPBOX
# ---------------------------------------------------------
def sincronizar_dropbox(sheet, dbx):
    print("🧹 Escaneando Dropbox para purgar archivos huérfanos...")
    try:
        registros = sheet.get_all_records()
        archivos_legales = []
        for r in registros:
            if not r.get('Pkg'): continue
            nombre = str(r.get('Nombre')).strip()
            version = str(r.get('Version')).strip()
            ruta_esperada = f"/{nombre}_{version}.apk".lower()
            archivos_legales.append(ruta_esperada)
        
        resultado = dbx.files_list_folder('')
        for entrada in resultado.entries:
            if isinstance(entrada, dropbox.files.FileMetadata):
                if entrada.path_lower not in archivos_legales:
                    print(f"🗑️ Eliminando huérfano de Dropbox: {entrada.path_display}")
                    try:
                        dbx.files_delete_v2(entrada.path_display)
                    except Exception as e:
                        print(f"   No se pudo borrar: {e}")
                         
    except Exception as e:
        print(f"Error en sincronización de Dropbox: {e}")

# ---------------------------------------------------------
# GENERADOR HTML Y JSON
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("🔄 Generando Sistema completo...")
    registros = sheet.get_all_records()
    
    obtainium_apps = []
    store_apps = []
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
        
        store_apps.append({
            "pkg": pkg,
            "name": nombre,
            "versionName": version,
            "versionCode": int(version_code) if version_code.isdigit() else 0,
            "apkUrl": link_apk,
            "icon": icono if icono else f"{REPO_URL_BASE}default_icon.png"
        })

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
    with open("obtainium.json", "w", encoding='utf-8') as f: 
        json.dump(export_data, f, indent=4)

    with open("store.json", "w", encoding='utf-8') as f: 
        json.dump(store_apps, f, indent=2, ensure_ascii=False)

    print(f"✅ JSONs generados: {len(store_apps)} apps")

    with open("index.html", "w", encoding='utf-8') as f: 
        f.write(f"<html><body><h1>V38 Online - Tienda APK</h1><p>Apps: {len(store_apps)}</p></body></html>")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor con Reconciliación Completa...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    try:
        # PASO 1: RECONCILIACIÓN (Detectar eliminados)
        reconciliar_todo(sheet, drive_service, dbx)
        
        # PASO 2: DETECTAR CAMBIOS DE NOMBRE
        detectar_cambios_nombre(sheet, drive_service)
        
        # PASO 3: PROCESAR NUEVAS APKs
        sheet = client_gs.open_by_key(SHEET_ID).sheet1  # Recargar
        registros = sheet.get_all_records()
        procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
        
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

        if nuevos:
            notificar(f"👷♂️ Procesando {len(nuevos)} APKs nuevas")
            
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
                    
                    link_icon = f"{REPO_URL_BASE}default_icon.png"

                    path = f"/{nombre}_{apk.version_name}.apk"
                    with open(temp_apk, "rb") as f: dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("dl=0", "dl=1")

                    sheet.append_row([
                        nombre, "Publicado", link_apk, apk.version_name, 
                        apk.package, link_icon, item['id'], "Dropbox", 
                        str(apk.version_code), calcular_hash(temp_apk), 
                        str(os.path.getsize(temp_apk))
                    ])
                    
                    print(f"✅ Agregado a Sheets: {nombre} v{apk.version_name}")
                    
                    time.sleep(2)
                    sheet = client_gs.open_by_key(SHEET_ID).sheet1
                    registros = sheet.get_all_records()
                    
                    # Limpieza de versiones anteriores por Package Name (Con buscador exacto)
                    try:
                        pkg_nuevo = str(apk.package).strip().lower()
                        for r in registros:
                            pkg_viejo = str(r.get('Pkg')).strip().lower()
                            id_viejo = str(r.get('ID Drive', '')).strip()
                            
                            if pkg_viejo == pkg_nuevo and id_viejo and id_viejo != item['id']:
                                try: drive_service.files().delete(fileId=id_viejo).execute()
                                except: pass
                                
                                try:
                                    celda = sheet.find(id_viejo)
                                    if celda:
                                        sheet.delete_row(celda.row)
                                        time.sleep(1.5)
                                except Exception as e:
                                    print(f"   ⚠️ Error borrando fila vieja: {e}")
                                    
                        sheet = client_gs.open_by_key(SHEET_ID).sheet1
                        registros = sheet.get_all_records()
                    except Exception as e:
                        print(f"   ⚠️ Error en limpieza de versiones: {e}")
                    
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                    time.sleep(2)
                    
                except Exception as e: 
                    print(f"[ERROR] Procesando {item['name']}: {e}")
                    notificar(f"❌ Error: {str(e)[:100]}")
                finally: 
                    if os.path.exists(temp_apk): os.remove(temp_apk)
        else:
            print("ℹ️ No hay APKs nuevas para procesar")
            
    except Exception as e: 
        print(f"[ERROR] En procesamiento: {e}")
        import traceback
        traceback.print_exc()
        notificar(f" Error: {str(e)[:100]}")

    restaurar_faltantes_en_dropbox(sheet, drive_service, dbx)
    
    time.sleep(2)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1

    sincronizar_dropbox(sheet, dbx)
    generar_sistema_completo(sheet)

    print("✅ Reconciliación Completa y Almacenamiento Optimizado.")

if __name__ == "__main__":
    main()
