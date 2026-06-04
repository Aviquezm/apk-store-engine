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
    print("🔄 INICIANDO RECONCILIACIÓN COMPLETA...")
    try:
        # 1. Obtener TODOS los archivos en Drive
        ids_en_drive = set()
        page_token = None
        while True:
            query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
            response = drive_service.files().list(q=query, pageSize=1000, fields="nextPageToken, files(id, name)", pageToken=page_token).execute()
            for item in response.get('files', []):
                if item['name'].lower().endswith('.apk'): ids_en_drive.add(item['id'])
            page_token = response.get('nextPageToken')
            if not page_token: break
                
        print(f"📁 Archivos APK válidos en Drive: {len(ids_en_drive)}")
        
        # 2. CUADRÍCULA EXACTA DEL EXCEL
        todas_las_filas = sheet.get_all_values()
        if not todas_las_filas: return
        encabezados = todas_las_filas[0]
        
        try:
            col_id_drive = encabezados.index('ID Drive')
            col_nombre = encabezados.index('Nombre')
            col_version = encabezados.index('Version')
        except ValueError:
            print("⚠️ Error: No se encontraron las columnas clave en la Fila 1 del Excel.")
            return

        print(f"📊 Filas reales detectadas en Sheets: {len(todas_las_filas)}")
        
        filas_a_eliminar = []
        
        # 3. Escanear fila por fila de forma absoluta
        for i, fila in enumerate(todas_las_filas):
            num_fila_real = i + 1
            if num_fila_real == 1: continue # Saltar fila 1 (Títulos)
            
            # Extraer datos previniendo errores si la fila está cortada
            id_drive = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
            nombre = str(fila[col_nombre]).strip() if col_nombre < len(fila) else ""
            version = str(fila[col_version]).strip() if col_version < len(fila) else ""
            
            if id_drive and id_drive not in ids_en_drive:
                print(f"🗑️ DETECTADO: '{nombre}' v{version} fue ELIMINADO de Drive.")
                filas_a_eliminar.append((num_fila_real, nombre, version))
        
        # 4. Eliminar de abajo hacia arriba
        if filas_a_eliminar:
            print(f"🧹 Limpiando {len(filas_a_eliminar)} archivos eliminados...")
            for fila_num, nombre, version in sorted(filas_a_eliminar, key=lambda x: x[0], reverse=True):
                # Borrar Dropbox
                ruta_dropbox = f"/{nombre}_{version}.apk".lower()
                try:
                    resultado = dbx.files_list_folder('')
                    for entrada in resultado.entries:
                        if isinstance(entrada, dropbox.files.FileMetadata) and entrada.path_lower == ruta_dropbox:
                            dbx.files_delete_v2(entrada.path_display)
                            print(f"   ✅ Dropbox: {entrada.path_display} eliminado")
                            break
                except Exception as e: print(f"   ⚠️ Error en Dropbox: {e}")
                
                # Borrar Fila Exacta en Excel
                try:
                    sheet.delete_row(fila_num)
                    print(f"   ✅ Sheets: Fila real {fila_num} eliminada.")
                    time.sleep(1.5)
                except Exception as e: print(f"   ⚠️ Error Sheets fila {fila_num}: {e}")
        else:
            print("✅ Todo está sincronizado. No hay eliminados que limpiar.")
            
    except Exception as e:
        print(f"[ERROR] En reconciliación: {e}")

# ---------------------------------------------------------
# DETECTAR CAMBIOS DE NOMBRE
# ---------------------------------------------------------
def detectar_cambios_nombre(sheet, drive_service):
    print("🔍 Detectando cambios de nombre...")
    try:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items_drive = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
        items_apk = [i for i in items_drive if i['name'].lower().endswith('.apk')]
        
        todas_las_filas = sheet.get_all_values()
        if not todas_las_filas: return
        encabezados = todas_las_filas[0]
        
        col_id_drive = encabezados.index('ID Drive')
        col_nombre = encabezados.index('Nombre')
        col_version = encabezados.index('Version')
        
        cambios_detectados = 0
        
        for item in items_apk:
            item_id = str(item['id']).strip()
            item_nombre_archivo = item['name']
            
            for i, fila in enumerate(todas_las_filas):
                num_fila_real = i + 1
                if num_fila_real == 1: continue
                
                id_sheet = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
                nombre_sheet = str(fila[col_nombre]).strip() if col_nombre < len(fila) else ""
                version_sheet = str(fila[col_version]).strip() if col_version < len(fila) else ""
                
                if id_sheet == item_id:
                    nombre_esperado = f"{nombre_sheet}_{version_sheet}.apk"
                    if item_nombre_archivo != nombre_esperado:
                        print(f"🔄 DETECTADO: Archivo cambió a '{item_nombre_archivo}'")
                        try:
                            # +1 porque las columnas en gspread empiezan en 1 (A=1)
                            sheet.update_cell(num_fila_real, col_nombre + 1, item_nombre_archivo)
                            print("   ✅ Nombre actualizado en Sheets")
                            cambios_detectados += 1
                        except Exception as e: print(f"   ⚠️ Error actualizando Sheets: {e}")
                    break
        if cambios_detectados == 0: print("✅ No hay cambios de nombre detectados")
    except Exception as e: print(f"[ERROR] Detectando cambios: {e}")

# ---------------------------------------------------------
# RESTAURACIÓN Y SINCRONIZACIÓN
# ---------------------------------------------------------
def restaurar_faltantes_en_dropbox(sheet, drive_service, dbx):
    print("🔄 Verificando integridad de Dropbox vs Excel...")
    try:
        resultado = dbx.files_list_folder('')
        archivos_dropbox = [e.path_display.lower() for e in resultado.entries if isinstance(e, dropbox.files.FileMetadata)]
        registros = sheet.get_all_records()
        for r in registros:
            if not r.get('Pkg'): continue
            nombre, version, id_drive = str(r.get('Nombre', '')).strip(), str(r.get('Version', '')).strip(), str(r.get('ID Drive', '')).strip()
            ruta_esperada = f"/{nombre}_{version}.apk"
            if ruta_esperada.lower() not in archivos_dropbox and id_drive:
                print(f"⚠️ Faltante detectado: {ruta_esperada}. Restaurando...")
                try:
                    request = drive_service.files().get_media(fileId=id_drive)
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    while not downloader.next_chunk()[1]: pass
                    fh.seek(0)
                    with open("temp.apk", "wb") as f: f.write(fh.read())
                    with open("temp.apk", "rb") as f: dbx.files_upload(f.read(), ruta_esperada, mode=WriteMode('overwrite'))
                    print(f"   ✅ Restaurado en Dropbox: {ruta_esperada}")
                except Exception as e: print(f"   ⚠️ Error restaurando: {e}")
        print("✅ Verificación terminada.")
    except Exception as e: print(f"[ERROR] Restaurando faltantes: {e}")

def sincronizar_dropbox(sheet, dbx):
    print("🧹 Escaneando Dropbox para purgar archivos huérfanos...")
    try:
        registros = sheet.get_all_records()
        archivos_legales = [f"/{str(r.get('Nombre')).strip()}_{str(r.get('Version')).strip()}.apk".lower() for r in registros if r.get('Pkg')]
        resultado = dbx.files_list_folder('')
        for e in resultado.entries:
            if isinstance(e, dropbox.files.FileMetadata) and e.path_lower not in archivos_legales:
                print(f"🗑️ Eliminando huérfano de Dropbox: {e.path_display}")
                try: dbx.files_delete_v2(e.path_display)
                except: pass
    except Exception as e: print(f"Error sincronización: {e}")

# ---------------------------------------------------------
# GENERADOR HTML Y JSON
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("🔄 Generando Sistema completo...")
    registros = sheet.get_all_records()
    obtainium_apps, store_apps, archivos_html_validos = [], [], ["index.html"]

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
        
        with open(filename, "w", encoding='utf-8') as f: 
            f.write(f'<!DOCTYPE html><html><head><title>{nombre}</title></head><body><h1>{nombre}</h1><p>Version: {version}</p><a href="{link_apk}">Descargar {nombre} v{version}</a></body></html>')
        
        obtainium_apps.append({"id": pkg, "url": link_apk, "name": nombre, "version": version, "pinned": False, "categories": [], "preferredApkPath": "", "additionalSettings": ""})
        store_apps.append({"pkg": pkg, "name": nombre, "versionName": version, "versionCode": int(version_code) if version_code.isdigit() else 0, "apkUrl": link_apk, "icon": icono if icono else f"{REPO_URL_BASE}default_icon.png"})

    for archivo in os.listdir('.'):
        if archivo.endswith('.html') and archivo not in archivos_html_validos:
            os.remove(archivo)
            print(f"🗑️ HTML huérfano eliminado: {archivo}")

    with open("obtainium.json", "w", encoding='utf-8') as f: json.dump({"debug": "BOT", "apps": obtainium_apps}, f, indent=4)
    with open("store.json", "w", encoding='utf-8') as f: json.dump(store_apps, f, indent=2, ensure_ascii=False)
    with open("index.html", "w", encoding='utf-8') as f: f.write(f"<html><body><h1>V38 Online - Tienda APK</h1><p>Apps: {len(store_apps)}</p></body></html>")
    print(f"✅ JSONs generados: {len(store_apps)} apps")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("🚀 Iniciando Motor...")
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    try:
        reconciliar_todo(sheet, drive_service, dbx)
        detectar_cambios_nombre(sheet, drive_service)
        
        # PROCESAR NUEVAS APKs
        sheet = client_gs.open_by_key(SHEET_ID).sheet1
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
                    while not downloader.next_chunk()[1]: pass
                    fh.seek(0)
                    with open(temp_apk, "wb") as f: f.write(fh.read())

                    apk = APK(temp_apk)
                    nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
                    path = f"/{nombre}_{apk.version_name}.apk"
                    
                    with open(temp_apk, "rb") as f: dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
                    l_apk = dbx.sharing_create_shared_link_with_settings(path).url
                    link_apk = l_apk.replace("dl=0", "dl=1")

                    sheet.append_row([
                        nombre, "Publicado", link_apk, apk.version_name, apk.package, 
                        f"{REPO_URL_BASE}default_icon.png", item['id'], "Dropbox", 
                        str(apk.version_code), calcular_hash(temp_apk), str(os.path.getsize(temp_apk))
                    ])
                    print(f"✅ Agregado a Sheets: {nombre}")
                    
                    time.sleep(2)
                    
                    # LIMPIEZA DE VERSIONES VIEJAS (CON CUADRÍCULA EXACTA)
                    try:
                        sheet = client_gs.open_by_key(SHEET_ID).sheet1
                        todas_las_filas = sheet.get_all_values()
                        encabezados = todas_las_filas[0]
                        col_pkg, col_id_drive = encabezados.index('Pkg'), encabezados.index('ID Drive')
                        
                        pkg_nuevo = str(apk.package).strip().lower()
                        filas_a_borrar = []
                        
                        for i, fila in enumerate(todas_las_filas):
                            num_fila_real = i + 1
                            if num_fila_real == 1: continue
                            
                            pkg_viejo = str(fila[col_pkg]).strip().lower() if col_pkg < len(fila) else ""
                            id_viejo = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
                            
                            if pkg_viejo == pkg_nuevo and id_viejo and id_viejo != item['id']:
                                try: drive_service.files().delete(fileId=id_viejo).execute()
                                except: pass
                                filas_a_borrar.append(num_fila_real)
                                
                        if filas_a_borrar:
                            for fila_num in sorted(filas_a_borrar, reverse=True):
                                try:
                                    sheet.delete_row(fila_num)
                                    time.sleep(1.5)
                                except Exception as e: print(f"   ⚠️ Error borrando fila vieja {fila_num}: {e}")
                    except Exception as e: print(f"   ⚠️ Error limpieza versiones: {e}")
                    
                    notificar(f"✅ {nombre} v{apk.version_name} listo")
                    time.sleep(2)
                except Exception as e: 
                    print(f"[ERROR] Procesando: {e}")
                    notificar(f"❌ Error: {str(e)[:100]}")
                finally: 
                    if os.path.exists(temp_apk): os.remove(temp_apk)
        else: print("ℹ️ No hay APKs nuevas")
    except Exception as e: print(f"[ERROR] En procesamiento: {e}")

    restaurar_faltantes_en_dropbox(sheet, drive_service, dbx)
    time.sleep(2)
    sincronizar_dropbox(sheet, dbx)
    generar_sistema_completo(sheet)
    print("✅ Motor Optimizado y Finalizado.")

if __name__ == "__main__":
    main()
