import os
import json
import io
import time
import shutil
import dropbox
import gspread
import zipfile
import re
import requests
from datetime import datetime
from PIL import Image
from dropbox.files import WriteMode
from dropbox.exceptions import ApiError
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
# 0. UTILIDADES
# ---------------------------------------------------------
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        requests.post(url, data=data)
    except Exception as e:
        print(f"⚠️ Error Telegram: {e}")

def limpiar_texto(texto):
    """Elimina espacios invisibles, saltos de línea y pone todo en minúsculas"""
    if not texto: return ""
    return str(texto).strip().lower().replace('\n', '').replace('\r', '').replace('\t', '')

# ---------------------------------------------------------
# 1. FUNCIÓN FORENSE (La que arregla el problema)
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        
        # Limpieza agresiva del paquete nuevo
        pkg_nuevo = limpiar_texto(pkg_nuevo_raw)
        
        print(f"\n🔍 INICIANDO ESCÁNER FORENSE para: '{pkg_nuevo}'")
        print(f"   (ID del archivo nuevo: {id_archivo_nuevo})")
        
        for i, r in enumerate(registros):
            # Limpieza agresiva del paquete viejo del Excel
            pkg_viejo = limpiar_texto(r.get('Pkg'))
            id_viejo = str(r.get('ID Drive', '')).strip()
            nombre_app = r.get('Nombre', 'App')
            
            # --- DEBUG VISUAL (Para ver por qué falla) ---
            # Solo imprimimos si se parecen un poco para no llenar el log
            if pkg_nuevo in pkg_viejo or pkg_viejo in pkg_nuevo:
                print(f"   👉 Fila {i+2}: Excel='{pkg_viejo}' vs Nuevo='{pkg_nuevo}' | IDs: {id_viejo} vs {id_archivo_nuevo}")

            # LA COMPARACIÓN MAESTRA
            if pkg_viejo == pkg_nuevo:
                if id_viejo != id_archivo_nuevo:
                    print(f"   🚨 ¡DUPLICADO DETECTADO! Fila {i+2} ({nombre_app})")
                    
                    # 1. Borrar de Drive
                    try:
                        drive_service.files().delete(fileId=id_viejo).execute()
                        print("      🔥 Drive: Eliminado.")
                    except: print("      ⚠️ Drive: No encontrado.")

                    # 2. Borrar de Dropbox
                    nombre_dbx = f"/{nombre_app.replace(' ', '_')}_v{r.get('Version')}.apk"
                    try:
                        dbx.files_delete_v2(nombre_dbx)
                        print("      🔥 Dropbox: Eliminado.")
                    except: pass

                    filas_a_borrar.append(i + 2)
                    archivos_borrados += 1
                else:
                    print(f"   ✅ Esta es la fila que acabamos de crear (Fila {i+2}). No borrar.")

        # 3. EJECUTAR BORRADO EN EXCEL
        if filas_a_borrar:
            print(f"🔪 Eliminando {len(filas_a_borrar)} filas antiguas del Excel...")
            # Borramos de abajo hacia arriba
            for fila_num in sorted(filas_a_borrar, reverse=True):
                sheet.delete_row(fila_num)
                print(f"   - Fila {fila_num} eliminada.")
                time.sleep(1.5)
        else:
            print("✅ Escáner terminado. No se encontraron duplicados antiguos.")

        return archivos_borrados

    except Exception as e:
        print(f"❌ Error Forense: {e}")
        return 0

# ---------------------------------------------------------
# 2. MOTOR DE EXTRACCIÓN
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            app_clean = app_name.lower().replace(" ", "")
            for nombre in archivos:
                nombre_lc = nombre.lower()
                if nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre:
                    if 'notification' in nombre_lc or 'abc_' in nombre_lc: continue
                    try:
                        data = z.read(nombre)
                        img = Image.open(io.BytesIO(data))
                        w, h = img.size
                        if abs(w - h) > 2: continue 
                        if not (120 <= w <= 1024): continue
                        puntuacion = 0
                        if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 5000
                        if 'app_icon' in nombre_lc or 'store_icon' in nombre_lc: puntuacion += 3000
                        if 'launcher' in nombre_lc:
                            puntuacion += 1000
                            if 'foreground' in nombre_lc or 'background' in nombre_lc: puntuacion -= 500 
                        if app_clean in nombre_lc: puntuacion += 800
                        if 'tc_' in nombre_lc: puntuacion += 400
                        if 'xxxhdpi' in nombre_lc: puntuacion += 300
                        elif 'xxhdpi' in nombre_lc: puntuacion += 200
                        if puntuacion > mejor_puntuacion:
                            mejor_puntuacion = puntuacion
                            mejor_data = data
                    except: continue
        return mejor_data
    except: return None

# ---------------------------------------------------------
# 3. SINCRONIZADOR
# ---------------------------------------------------------
def sincronizar_todo(sheet):
    print("🔄 Sincronizando catálogo...")
    registros = sheet.get_all_records()
    nuevo_index = {
        "repo": {"name": "Mi Tienda Privada", "description": "APKs VIP", "address": REPO_URL, "icon": f"{REPO_URL}icon.png"}, 
        "apps": []
    }
    apps_dict = {}
    for r in registros:
        pkg = r.get('Pkg')
        if not pkg: continue
        entry = {
            "versionName": str(r.get('Version')),
            "versionCode": str(r.get('Version Code', '0')),
            "downloadURL": r.get('Link APK'),
            "added": datetime.now().strftime("%Y-%m-%d")
        }
        if pkg not in apps_dict:
            apps_dict[pkg] = {
                "name": r.get('Nombre'),
                "packageName": pkg,
                "suggestedVersionName": str(r.get('Version')),
                "icon": r.get('Link Icono'),
                "versions": [entry]
            }
        else:
            if not any(v['versionName'] == entry['versionName'] for v in apps_dict[pkg]['versions']):
                apps_dict[pkg]['versions'].insert(0, entry)

    nuevo_index["apps"] = list(apps_dict.values())
    with open("index.json", "w") as f: json.dump(nuevo_index, f, indent=4)

# ---------------------------------------------------------
# 4. MAIN
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
    print("🚀 Iniciando Motor V16 (Forense)...")
    
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

    if not nuevos:
        print("💤 Sin novedades.")
        return

    notificar(f"👷‍♂️ <b>Hola Jefe</b>\nAnalizando <b>{len(nuevos)}</b> APKs nuevas con Modo Forense.")

    for item in nuevos:
        file_id = str(item['id']).strip()
        file_name = item['name']
        print(f"⚙️ Procesando: {file_name}")
        notificar(f"⚙️ Analizando: <i>{file_name}</i>")
        
        temp_apk = "temp.apk"
        try:
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open(temp_apk, "wb") as f: f.write(fh.read())

            apk = APK(temp_apk)
            nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
            package_name = apk.package
            
            # Subir y Guardar
            nombre_final = f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
            
            icon_data = extraer_icono_precision(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                url_subida = subir_a_dropbox(dbx, icon_filename, icon_filename)
                if url_subida: link_icon = url_subida
                os.remove(icon_filename)

            sheet.append_row([
                nombre_limpio, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            print("✅ Guardado en Excel. Iniciando limpieza...")

            # --- LA PRUEBA DE LA VERDAD ---
            # Aquí llamamos al forense para que nos diga por qué no borraba antes
            borrados = eliminar_rastros_anteriores(sheet, drive_service, dbx, package_name, file_id)
            
            msj = (
                f"✅ <b>¡Actualizado!</b>\n"
                f"📦 {nombre_limpio} v{apk.version_name}\n"
                f"🗑️ Duplicados eliminados: {borrados}"
            )
            notificar(msj)

        except Exception as e:
            notificar(f"❌ Error: {e}")
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_todo(sheet)
    notificar("🏁 Fin del proceso.")

if __name__ == "__main__":
    main()
