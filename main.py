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

# --- CONFIGURACIÓN DE SECRETOS ---
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
# 0. NOTIFICACIONES
# ---------------------------------------------------------
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        requests.post(url, data=data)
    except Exception as e:
        print(f"⚠️ Error Telegram: {e}")

# ---------------------------------------------------------
# 1. FUNCIÓN EXTERMINADORA (Borra Drive, Dropbox y Excel)
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, package_name, id_archivo_nuevo):
    """
    Busca cualquier rastro de versiones anteriores de este paquete y los destruye.
    """
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        
        print(f"🧹 Escaneando versiones antiguas de: {package_name}...")
        
        # Iteramos buscando duplicados
        for i, r in enumerate(registros):
            # Si el paquete coincide Y el ID de Drive es diferente (es decir, es una versión vieja)
            if r.get('Pkg') == package_name and str(r.get('ID Drive')) != id_archivo_nuevo:
                
                nombre_app = r.get('Nombre')
                version_app = r.get('Version')
                print(f"🚫 Detectada versión obsoleta: {nombre_app} v{version_app}")

                # 1. Borrar de Google Drive
                id_viejo = str(r.get('ID Drive'))
                try:
                    drive_service.files().delete(fileId=id_viejo).execute()
                    print("   - 🔥 Drive: Archivo eliminado.")
                except:
                    print("   - ⚠️ Drive: No se encontró el archivo (quizás ya borrado).")

                # 2. Borrar de Dropbox
                # Reconstruimos el nombre del archivo en Dropbox para borrarlo
                # Formato: Nombre_vVersion.apk
                nombre_dbx = f"/{nombre_app.replace(' ', '_')}_v{version_app}.apk"
                try:
                    dbx.files_delete_v2(nombre_dbx)
                    print(f"   - 🔥 Dropbox: {nombre_dbx} eliminado.")
                except:
                    print(f"   - ⚠️ Dropbox: No se pudo borrar {nombre_dbx} (quizás nombre diferente).")

                # Marcamos la fila para borrado (Index + 2 porque sheet empieza en 1 y tiene header)
                filas_a_borrar.append(i + 2)
                archivos_borrados += 1

        # 3. Borrar filas del Excel (De abajo hacia arriba para no romper índices)
        for fila_num in sorted(filas_a_borrar, reverse=True):
            sheet.delete_row(fila_num)
            print(f"   - 🔥 Excel: Fila {fila_num} eliminada.")
            time.sleep(1) # Pausa de seguridad para gspread

        return archivos_borrados

    except Exception as e:
        print(f"⚠️ Error en limpieza: {e}")
        return 0

# ---------------------------------------------------------
# 2. MOTOR DE EXTRACCIÓN (El Destripador v8)
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
    print("🚀 Iniciando Motor V14 (Modo Exterminador)...")
    
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    registros = sheet.get_all_records()
    procesados = {str(r.get('ID Drive')).strip() for r in registros if r.get('ID Drive')}
    
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])
    
    # Solo procesamos lo que NO está en el Excel
    nuevos = [i for i in items if i['name'].lower().endswith('.apk') and str(i['id']).strip() not in procesados]

    if not nuevos:
        print("💤 Nada nuevo que procesar.")
        return

    notificar(f"👷‍♂️ <b>Hola Jefe</b>\nDetectados <b>{len(nuevos)}</b> APKs nuevas. Iniciando protocolo de limpieza y subida.")

    for item in nuevos:
        file_id = str(item['id']).strip()
        file_name = item['name']
        print(f"⚙️ Procesando: {file_name}")
        notificar(f"⚙️ Analizando: <i>{file_name}</i>...")
        
        temp_apk = "temp.apk"
        try:
            # 1. Descargar para analizar
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
            
            # 2. PROTOCOLO EXTERMINADOR: Borrar todo lo viejo ANTES de subir lo nuevo
            # Así garantizamos que no haya duplicados ni un segundo.
            borrados = eliminar_rastros_anteriores(sheet, drive_service, dbx, package_name, file_id)
            if borrados > 0:
                notificar(f"🗑️ <b>Limpieza:</b> Se eliminaron {borrados} versiones antiguas de {nombre_limpio}.")

            # 3. Extracción de Icono
            icon_data = extraer_icono_precision(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            
            # 4. Subida a Dropbox
            nombre_final = f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
            
            link_icon = "https://via.placeholder.com/150"
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                url_subida = subir_a_dropbox(dbx, icon_filename, icon_filename)
                if url_subida: link_icon = url_subida
                os.remove(icon_filename)

            # 5. Guardar en Excel
            sheet.append_row([
                nombre_limpio, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            
            # 6. Reporte Final
            msj = (
                f"✅ <b>¡Actualizado!</b>\n"
                f"📦 <b>{nombre_limpio}</b>\n"
                f"🆙 Nueva versión: v{apk.version_name}\n"
                f"🔗 <a href='{link_apk}'>Descargar</a>"
            )
            notificar(msj)
            print(f"✅ Éxito: {nombre_limpio}")

        except Exception as e:
            notificar(f"❌ <b>Error Crítico</b> con {file_name}:\n<code>{str(e)}</code>")
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_todo(sheet)
    notificar("🏁 <b>Ciclo Terminado.</b> Tienda limpia y actualizada.")

if __name__ == "__main__":
    main()
