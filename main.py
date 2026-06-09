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

def expulsar_de_drive(drive_service, file_id, folder_id):
    try:
        drive_service.files().delete(fileId=file_id).execute()
    except:
        try:
            drive_service.files().update(fileId=file_id, removeParents=folder_id).execute()
        except:
            pass

# ---------------------------------------------------------
# RECONCILIACIÓN TOTAL
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
    
    todas_las_filas = sheet.get_all_values()
    if len(todas_las_filas) <= 1:
        return
    
    encabezados = todas_las_filas[0]
    datos_actuales = todas_las_filas[1:]
    
    col_id_drive = encabezados.index('ID Drive')
    col_nombre = encabezados.index('Nombre')
    col_version = encabezados.index('Version') 
    col_link = encabezados.index('Link APK')
    
    datos_filtrados = []
    
    for fila in datos_actuales:
        id_fila = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
        
        if id_fila in ids_en_drive:
            datos_filtrados.append(fila)
        else:
            nombre = str(fila[col_nombre]).strip()
            version = str(fila[col_version]).strip()
            link = str(fila[col_link]).strip()
            
            notificar(f"🗑️ <b>Versión eliminada:</b> Se detectó que <i>{nombre} v{version}</i> ya no está en Drive. Limpiando...")
            
            try:
                link_limpio = link.replace("dl=1", "dl=0")
                metadata = dbx.sharing_get_shared_link_metadata(link_limpio)
                if metadata and metadata.path_lower:
                    dbx.files_delete_v2(metadata.path_lower)
            except:
                pass

    sheet.clear()
    sheet.append_row(encabezados)
    if datos_filtrados:
        sheet.append_rows(datos_filtrados)

# ---------------------------------------------------------
# DETECTAR CAMBIOS DE NOMBRE
# ---------------------------------------------------------
def detectar_cambios_nombre(sheet, drive_service):
    print("--- DETECTANDO CAMBIOS DE NOMBRE ---")
    
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
            
    todas_las_filas = sheet.get_all_values()
    if len(todas_las_filas) <= 1: return
    
    encabezados = todas_las_filas[0]
    col_id_drive = encabezados.index('ID Drive')
    col_nombre = encabezados.index('Nombre')
    
    for item in ids_en_drive:
        id_drive_actual = str(item['id']).strip()
        nombre_limpio_drive = re.sub(r'([ _]v?\d+.*\.apk|\.apk)$', '', item['name'], flags=re.IGNORECASE).strip()
        
        for i, fila in enumerate(todas_las_filas):
            if i == 0: continue
            id_sheet = str(fila[col_id_drive]).strip() if col_id_drive < len(fila) else ""
            nombre_sheet = str(fila[col_nombre]).strip() if col_nombre < len(fila) else ""
            
            if id_sheet == id_drive_actual:
                if nombre_sheet != nombre_limpio_drive:
                    sheet.update_cell(i + 1, col_nombre + 1, nombre_limpio_drive)
                    notificar(f"🔄 <b>Nombre modificado:</b> La app <i>'{nombre_sheet}'</i> ahora se llama <b>'{nombre_limpio_drive}'</b>")
                break

# ---------------------------------------------------------
# PROCESAMIENTO Y LIMPIEZA INTELIGENTE
# ---------------------------------------------------------
def procesar_y_generar(sheet, drive_service, dbx):
    print("--- PROCESANDO NUEVAS APPS Y GENERANDO JSON ---")
    
    registros = sheet.get_all_records()
    procesados = [str(r['ID Drive']).strip() for r in registros if r.get('ID Drive')]
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    
    nuevos_items = [item for item in items if item['name'].lower().endswith('.apk') and str(item['id']).strip() not in procesados]
    
    if nuevos_items:
        notificar(f"👷‍♂️ <b>¡Procesando {len(nuevos_items)} archivo(s) nuevo(s)!</b>")
    
    for item in nuevos_items:
        id_item = str(item['id']).strip()
        nombre_base = re.sub(r'([ _]v?\d+.*\.apk|\.apk)$', '', item['name'], flags=re.IGNORECASE).strip()
        
        notificar(f"⏳ Extrayendo y analizando: <b>{nombre_base}</b>...")
        
        request = drive_service.files().get_media(fileId=item['id'])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        while not downloader.next_chunk()[1]: pass
        fh.seek(0)
        
        with open("temp.apk", "wb") as f:
            f.write(fh.read())
        
        try:
            apk = APK("temp.apk")
            nuevo_pkg = apk.package
            nueva_version = apk.version_name
            nuevo_version_code = int(apk.version_code) if apk.version_code else 0
        except Exception as e:
            notificar(f"❌ <b>Error de Lectura:</b> No se pudo analizar <b>{nombre_base}</b>. El archivo está corrupto. Expulsando de Drive...")
            os.remove("temp.apk")
            expulsar_de_drive(drive_service, id_item, DRIVE_FOLDER_ID)
            continue

        nombre_final = re.sub(r'([ _]v?\d+.*\.apk|\.apk)$', '', item['name'], flags=re.IGNORECASE).strip()
        
        es_actualizacion_valida = True
        es_nueva_app = True

        registros_actualizados = sheet.get_all_records()
        for i, r in enumerate(registros_actualizados):
            if r.get('Pkg') == nuevo_pkg:
                es_nueva_app = False
                
                viejo_code_str = str(r.get('Version Code')).strip()
                viejo_version_code = int(viejo_code_str) if viejo_code_str.isdigit() else 0
                
                if nuevo_version_code > viejo_version_code:
                    old_id_drive = str(r.get('ID Drive')).strip()
                    old_link = str(r.get('Link APK')).strip()
                    old_version = str(r.get('Version')).strip()
                    
                    notificar(f"🧹 <b>Actualización detectada:</b> Reemplazando v{old_version} por v{nueva_version}...")
                    
                    if old_id_drive:
                        expulsar_de_drive(drive_service, old_id_drive, DRIVE_FOLDER_ID)
                    
                    if old_link:
                        try:
                            link_limpio = old_link.replace("dl=1", "dl=0")
                            metadata = dbx.sharing_get_shared_link_metadata(link_limpio)
                            if metadata and metadata.path_lower:
                                dbx.files_delete_v2(metadata.path_lower)
                        except: pass
                    
                    try: sheet.delete_rows(i + 2)
                    except: pass
                else:
                    es_actualizacion_valida = False
                    notificar(f"⚠️ <b>Rechazo automático:</b> Se detectó {nombre_final} v{nueva_version}, pero la tienda ya tiene una versión igual o superior. Eliminando archivo de Drive...")
                    expulsar_de_drive(drive_service, id_item, DRIVE_FOLDER_ID)
                    
                break 
        
        if es_actualizacion_valida:
            path = f"/{nombre_final}_{nueva_version}.apk"
            
            with open("temp.apk", "rb") as f:
                dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))
            
            link = dbx.sharing_create_shared_link_with_settings(path).url.replace("dl=0", "dl=1")
            
            sheet.append_row([
                nombre_final, "Publicado", link, nueva_version, 
                nuevo_pkg, id_item, "Dropbox", str(nuevo_version_code), 
                calcular_hash("temp.apk"), str(os.path.getsize("temp.apk"))
            ])
            
            if es_nueva_app:
                notificar(f"🎉 <b>¡Nueva App Agregada!</b> {nombre_final} v{nueva_version} se ha unido a tu catálogo.")
            else:
                notificar(f"✅ <b>App Actualizada con éxito:</b> {nombre_final} ya está en la v{nueva_version}.")
        
        os.remove("temp.apk")

    registros_finales = sheet.get_all_records()
    
    lista_obtainium = []
    lista_store = []
    
    for r in registros_finales:
        if r.get('Pkg'):
            
            # 🚀 LÓGICA PARA CONVERTIR BYTES A MEGABYTES DIRECTAMENTE DESDE EL EXCEL
            peso_str = ""
            keys = list(r.keys())
            if len(keys) >= 10: # Si existe la columna 10 (que es donde guardamos el tamaño)
                val = str(r[keys[9]]).strip()
                if val.isdigit():
                    mb = round(int(val) / (1024 * 1024), 1)
                    peso_str = f"{mb} MB"
            
            lista_obtainium.append({
                "id": r['Pkg'],
                "url": r['Link APK'].replace("dl=0", "dl=1"),
                "name": r['Nombre'],
                "version": str(r['Version']),
                "pinned": False
            })
            
            lista_store.append({
                "pkg": r['Pkg'],
                "name": r['Nombre'],
                "versionName": str(r['Version']),
                "versionCode": int(r['Version Code'] if str(r['Version Code']).isdigit() else 0),
                "apkUrl": r['Link APK'].replace("dl=0", "dl=1"),
                "icon": "",
                "size": peso_str # 👈 AQUÍ SE INYECTA EL TAMAÑO AL JSON
            })
            
    with open("obtainium.json", "w", encoding='utf-8') as f:
        json.dump({"debug": "BOT", "apps": lista_obtainium}, f, indent=4)
        
    with open("store.json", "w", encoding='utf-8') as f:
        json.dump(lista_store, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    print("🚀 Iniciando Motor...")
    
    dbx = dropbox.Dropbox(app_key=DBX_KEY, app_secret=DBX_SECRET, oauth2_refresh_token=DBX_REFRESH_TOKEN)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(SHEET_ID).sheet1
    
    reconciliar_todo(sheet, drive_service, dbx)
    detectar_cambios_nombre(sheet, drive_service)
    procesar_y_generar(sheet, drive_service, dbx)
