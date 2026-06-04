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
    if not TG_TOKEN or not TG_CHAT_ID: 
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                       data={"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"})
    except: 
        pass

def calcular_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        while True:
            data = f.read(4096)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()

def nombre_seguro(texto):
    return re.sub(r'[^a-zA-Z0-9]', '_', str(texto).strip().lower())

# ---------------------------------------------------------
# RECONCILIACIÓN TOTAL (SOBREESCRITURA SEGURA)
# ---------------------------------------------------------
def reconciliar_todo(sheet, drive_service, dbx):
    print("--- INICIANDO RECONCILIACIÓN ---")
    
    ids_en_drive = []
    page_token = None
    while True:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        response = drive_service.files().list(q=query, pageSize=1000, fields="nextPageToken, files(id, name)", pageToken=page_token).execute()
        for item in response.get('files', []):
            if item['name'].lower().endswith('.apk'):
                ids_en_drive.append(item['id'])
        page_token = response.get('nextPageToken')
        if not page_token: break
    
    print(f"DEBUG: Se encontraron {len(ids_en_drive)} APKs válidas en Drive.")

    todas_las_filas = sheet.get_all_values()
    if len(todas_las_filas) <= 1:
        print("DEBUG: El Excel está vacío o solo tiene encabezados.")
        return
    
    encabezados = todas_las_filas[0]
    datos_actuales = todas_las_filas[1:]
    col_id_drive = encabezados.index('ID Drive')
    col_nombre = encabezados.index('Nombre')
    col_version = encabezados.index('Version')
    
    datos_filtrados = []
    
    for fila in datos_actuales:
        id_fila = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
        
        if id_fila in ids_en_drive:
            datos_filtrados.append(fila)
        else:
            nombre = str(fila[col_nombre]).strip()
            version = str(fila[col_version]).strip()
            print(f"DEBUG: La app '{nombre}' fue borrada de Drive. Eliminando...")
            
            ruta_dropbox = f"/{nombre}_{version}.apk".lower()
            try:
                dbx.files_delete_v2(ruta_dropbox)
                print(f"   ✅ Dropbox: {ruta_dropbox} eliminado.")
            except:
                pass

    print("DEBUG: Actualizando Excel por sobreescritura...")
    sheet.clear()
    sheet.append_row(encabezados)
    if datos_filtrados:
        sheet.append_rows(datos_filtrados)
        print(f"✅ Excel sincronizado con {len(datos_filtrados)} registros limpios.")

# ---------------------------------------------------------
# DETECTAR CAMBIOS DE NOMBRE (CON TIJERAS INTELIGENTES)
# ---------------------------------------------------------
def detectar_cambios_nombre(sheet, drive_service):
    print("--- DETECTANDO CAMBIOS DE NOMBRE ---")
    
    # 1. Obtener archivos desde Drive
    ids_en_drive = []
    page_token = None
    while True:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        response = drive_service.files().list(q=query, pageSize=1000, fields="nextPageToken, files(id, name)", pageToken=page_token).execute()
        for item in response.get('files', []):
            if item['name'].lower().endswith('.apk'):
                ids_en_drive.append(item)
        page_token = response.get('nextPageToken')
        if not page_token: break
            
    # 2. Leer Excel
    todas_las_filas = sheet.get_all_values()
    if len(todas_las_filas) <= 1: return
    
    encabezados = todas_las_filas[0]
    col_id_drive = encabezados.index('ID Drive')
    col_nombre = encabezados.index('Nombre')
    
    for item in ids_en_drive:
        id_drive_actual = str(item['id']).strip()
        
        # ✂️ LAS TIJERAS: Recorta " _v4.99.apk" o " v6.5.apk" y deja solo el nombre real
        nombre_limpio_drive = re.sub(r'([ _]v?\d+.*\.apk|\.apk)$', '', item['name'], flags=re.IGNORECASE).strip()
        
        for i, fila in enumerate(todas_las_filas):
            if i == 0: continue
            id_sheet = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
            nombre_sheet = str(fila[col_nombre]).strip() if col_nombre < len(fila) else ""
            
            # Si el ID coincide, comparamos los nombres limpios
            if id_sheet == id_drive_actual:
                if nombre_sheet != nombre_limpio_drive:
                    print(f"🔄 Cambio detectado: De '{nombre_sheet}' a '{nombre_limpio_drive}'")
                    sheet.update_cell(i + 1, col_nombre + 1, nombre_limpio_drive)
                    print(f"   ✅ Excel actualizado.")
                break

# ---------------------------------------------------------
# PROCESAMIENTO Y GENERACIÓN (PASO A PASO)
# ---------------------------------------------------------
def procesar_y_generar(sheet, drive_service, dbx):
    print("--- PROCESANDO NUEVAS APPS Y GENERANDO JSON ---")
    
    # 1. Obtener registros existentes
    registros = sheet.get_all_records()
    procesados = []
    for r in registros:
        if r.get('ID Drive'):
            procesados.append(str(r['ID Drive']).strip())
    
    # 2. Buscar nuevos en Drive
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    
    for item in items:
        id_item = str(item['id']).strip()
        if item['name'].lower().endswith('.apk') and id_item not in procesados:
            print(f"👷‍♂️ Procesando Nueva App: {item['name']}")
            
            request = drive_service.files().get_media(fileId=item['id'])
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            while not downloader.next_chunk()[1]: pass
            fh.seek(0)
            
            with open("temp.apk", "wb") as f:
                f.write(fh.read())
            
            apk = APK("temp.apk")
            
            # Al ser nueva, si tú ya le pusiste nombre bonito en Drive, usamos las mismas tijeras
            nombre = re.sub(r'([ _]v?\d+.*\.apk|\.apk)$', '', item['name'], flags=re.IGNORECASE).strip()
            path = f"/{nombre}_{apk.version_name}.apk"
            
            with open("temp.apk", "rb") as f:
                dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
            
            link = dbx.sharing_create_shared_link_with_settings(path).url.replace("dl=0", "dl=1")
            
            sheet.append_row([
                nombre, "Publicado", link, apk.version_name, 
                apk.package, "", # <--- CAMBIO AQUÍ: Celda vacía para que Android Studio lo maneje
                id_item, "Dropbox", str(apk.version_code), 
                calcular_hash("temp.apk"), str(os.path.getsize("temp.apk"))
            ])
            os.remove("temp.apk")
            print(f"   ✅ App {nombre} v{apk.version_name} lista y agregada.")

    # 3. Generar JSONs (Leemos fresco del Excel por si hubo cambios de nombre)
    print("DEBUG: Escribiendo JSONs finales...")
    registros_finales = sheet.get_all_records()
    
    lista_obtainium = []
    lista_store = []
    
    for r in registros_finales:
        if r.get('Pkg'):
            lista_obtainium.append({
                "id": r['Pkg'],
                "url": r['Link APK'].replace("dl=0", "dl=1"),
                "name": r['Nombre'],
                "version": r['Version'],
                "pinned": False
            })
            
            lista_store.append({
                "pkg": r['Pkg'],
                "name": r['Nombre'],
                "versionName": r['Version'],
                "versionCode": int(r['Version Code'] if str(r['Version Code']).isdigit() else 0),
                "apkUrl": r['Link APK'].replace("dl=0", "dl=1"),
                "icon": str(r.get('Icono', '')).strip() # <--- CAMBIO AQUÍ: Envía lo que tenga el Excel (vacío si no hay nada)
            })
            
    with open("obtainium.json", "w", encoding='utf-8') as f:
        json.dump({"debug": "BOT", "apps": lista_obtainium}, f, indent=4)
        
    with open("store.json", "w", encoding='utf-8') as f:
        json.dump(lista_store, f, indent=2, ensure_ascii=False)
        
    print(f"✅ Archivos JSON generados correctamente con {len(lista_store)} apps.")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Iniciando Motor...")
    
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    
    reconciliar_todo(sheet, drive_service, dbx)
    detectar_cambios_nombre(sheet, drive_service)
    procesar_y_generar(sheet, drive_service, dbx)
    
    print("--- PROCESO FINALIZADO ---")
