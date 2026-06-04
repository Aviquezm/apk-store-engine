import os
import json
import io
import time
import hashlib
import dropbox
import gspread
import re
import requests
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
        # Leemos en bloques para no saturar la memoria
        while True:
            data = f.read(4096)
            if not data:
                break
            sha256.update(data)
    return sha256.hexdigest()

def nombre_seguro(texto):
    return re.sub(r'[^a-zA-Z0-9]', '_', str(texto).strip().lower())

# ---------------------------------------------------------
# RECONCILIACIÓN TOTAL (EXPLICADA PASO A PASO)
# ---------------------------------------------------------
def reconciliar_todo(sheet, drive_service, dbx):
    print("--- INICIANDO RECONCILIACIÓN ---")
    
    # 1. Obtener archivos desde Drive
    ids_en_drive = []
    page_token = None
    while True:
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        response = drive_service.files().list(q=query, pageSize=1000, fields="nextPageToken, files(id, name)", pageToken=page_token).execute()
        
        for item in response.get('files', []):
            if item['name'].lower().endswith('.apk'):
                ids_en_drive.append(item['id'])
        
        page_token = response.get('nextPageToken')
        if not page_token:
            break
    
    print(f"DEBUG: Se encontraron {len(ids_en_drive)} APKs en Drive.")

    # 2. Obtener datos del Excel
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
    
    # 3. Filtrar
    for fila in datos_actuales:
        # Verificación manual de existencia
        id_fila = str(fila[col_id_drive]).strip()
        
        if id_fila in ids_en_drive:
            # Si existe, la mantenemos en la lista
            datos_filtrados.append(fila)
        else:
            # Si NO existe, notificamos y borramos de Dropbox
            nombre = str(fila[col_nombre]).strip()
            version = str(fila[col_version]).strip()
            print(f"DEBUG: La app {nombre} no está en Drive. Eliminando...")
            
            ruta_dropbox = f"/{nombre}_{version}.apk".lower()
            try:
                dbx.files_delete_v2(ruta_dropbox)
                print(f"✅ Dropbox: {ruta_dropbox} eliminado.")
            except:
                print(f"⚠️ Dropbox: No se pudo borrar {ruta_dropbox}")

    # 4. Sobreescribir el Excel con los datos limpios
    print("DEBUG: Actualizando Excel...")
    sheet.clear()
    sheet.append_row(encabezados)
    if datos_filtrados:
        sheet.append_rows(datos_filtrados)
        print(f"✅ Excel actualizado con {len(datos_filtrados)} registros.")

# ---------------------------------------------------------
# PROCESAMIENTO Y GENERACIÓN (PASO A PASO)
# ---------------------------------------------------------
def procesar_y_generar(sheet, drive_service, dbx):
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
            print(f"--- PROCESANDO NUEVA APP: {item['name']} ---")
            
            # Descargar
            request = drive_service.files().get_media(fileId=item['id'])
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            while not downloader.next_chunk()[1]: pass
            fh.seek(0)
            
            with open("temp.apk", "wb") as f:
                f.write(fh.read())
            
            # Analizar
            apk = APK("temp.apk")
            nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
            path = f"/{nombre}_{apk.version_name}.apk"
            
            # Subir a Dropbox
            with open("temp.apk", "rb") as f:
                dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
            
            link = dbx.sharing_create_shared_link_with_settings(path).url.replace("dl=0", "dl=1")
            
            # Agregar al Excel
            sheet.append_row([
                nombre, "Publicado", link, apk.version_name, 
                apk.package, f"{REPO_URL_BASE}default_icon.png", 
                id_item, "Dropbox", str(apk.version_code), 
                calcular_hash("temp.apk"), str(os.path.getsize("temp.apk"))
            ])
            os.remove("temp.apk")
            print(f"✅ App {nombre} agregada exitosamente.")

    # 3. Generar JSONs (Formato detallado)
    registros_finales = sheet.get_all_records()
    
    # Construir JSON para Obtainium
    lista_obtainium = []
    for r in registros_finales:
        if r.get('Pkg'):
            lista_obtainium.append({
                "id": r['Pkg'],
                "url": r['Link APK'].replace("dl=0", "dl=1"),
                "name": r['Nombre'],
                "version": r['Version'],
                "pinned": False
            })
            
    # Construir JSON para tu App
    lista_store = []
    for r in registros_finales:
        if r.get('Pkg'):
            lista_store.append({
                "pkg": r['Pkg'],
                "name": r['Nombre'],
                "versionName": r['Version'],
                "versionCode": int(r['Version Code'] if str(r['Version Code']).isdigit() else 0),
                "apkUrl": r['Link APK'].replace("dl=0", "dl=1"),
                "icon": r['Icono'] if r.get('Icono') else f"{REPO_URL_BASE}default_icon.png"
            })
            
    with open("obtainium.json", "w", encoding='utf-8') as f:
        json.dump({"debug": "BOT", "apps": lista_obtainium}, f, indent=4)
        
    with open("store.json", "w", encoding='utf-8') as f:
        json.dump(lista_store, f, indent=2, ensure_ascii=False)
        
    print("✅ JSONs generados correctamente.")

# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Iniciando Motor...")
    
    # Conexiones
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    
    # Ejecución explícita
    reconciliar_todo(sheet, drive_service, dbx)
    procesar_y_generar(sheet, drive_service, dbx)
    
    print("--- PROCESO FINALIZADO ---")
