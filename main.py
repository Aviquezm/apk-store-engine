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
# 1. FUNCIÓN NUCLEAR (Depurada)
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo, id_archivo_nuevo):
    """
    Busca versiones viejas comparando el Package Name de forma insensible a mayúsculas/espacios.
    """
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        
        # Normalizamos el paquete nuevo (minúsculas y sin espacios)
        pkg_nuevo_clean = str(pkg_nuevo).strip().lower()
        print(f"\n☢️ INICIANDO BARRIDO NUCLEAR para: [{pkg_nuevo_clean}]")
        
        for i, r in enumerate(registros):
            # Obtener datos de la fila
            pkg_viejo = str(r.get('Pkg', '')).strip().lower()
            id_viejo = str(r.get('ID Drive', '')).strip()
            nombre_app = r.get('Nombre', 'App')
            version_app = r.get('Version', '0.0')

            # Debug visual para entender qué pasa
            # print(f"   - Comparando con fila {i+2}: [{pkg_viejo}] vs [{pkg_nuevo_clean}]")

            # CRITERIO DE MUERTE:
            # 1. El paquete es el mismo.
            # 2. El ID de Drive NO es el que acabamos de subir (para no borrarnos a nosotros mismos).
            if pkg_viejo == pkg_nuevo_clean and id_viejo != id_archivo_nuevo:
                
                print(f"   🚨 ENCONTRADO DUPLICADO: {nombre_app} v{version_app} (Fila {i+2})")

                # 1. Borrar de Google Drive
                if id_viejo:
                    try:
                        drive_service.files().delete(fileId=id_viejo).execute()
                        print("      🔥 Drive: Archivo eliminado.")
                    except Exception as e:
                        print(f"      ⚠️ Drive: Falló borrado (¿ya no existe?): {e}")

                # 2. Borrar de Dropbox
                # Intentamos adivinar el nombre con la lógica estándar
                nombre_dbx = f"/{nombre_app.replace(' ', '_')}_v{version_app}.apk"
                try:
                    dbx.files_delete_v2(nombre_dbx)
                    print(f"      🔥 Dropbox: {nombre_dbx} eliminado.")
                except:
                    # Si falla, no importa, lo importante es borrar la fila del Excel
                    print(f"      ⚠️ Dropbox: No se encontró {nombre_dbx}, saltando.")

                # Agregamos a la lista de ejecución (Index + 2)
                filas_a_borrar.append(i + 2)
                archivos_borrados += 1

        # 3. EJECUTAR BORRADO DE FILAS (Inverso)
        if filas_a_borrar:
            print(f"🔪 Eliminando {len(filas_a_borrar)} filas del Excel...")
            for fila_num in sorted(filas_a_borrar, reverse=True):
                try:
                    sheet.delete_row(fila_num)
                    print(f"   - Fila {fila_num} eliminada.")
                    time.sleep(1.5) # Pausa obligatoria para no saturar Google API
                except Exception as e:
                    print(f"   ❌ Error borrando fila {fila_num}: {e}")
        else:
            print("✅ No se encontraron versiones antiguas para borrar.")

        return archivos_borrados

    except Exception as e:
        print(f"❌ Error CRÍTICO en limpieza: {e}")
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
    print("🔄 Sincronizando index.json...")
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
# 4. MAIN & DROPBOX
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
    print("🚀 Iniciando Motor V15 (Nuclear Debug)...")
    
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

    notificar(f"👷‍♂️ <b>Hola Jefe</b>\nProcesando <b>{len(nuevos)}</b> APK(s)...")

    for item in nuevos:
        file_id = str(item['id']).strip()
        file_name = item['name']
        print(f"⚙️ Analizando: {file_name}")
        notificar(f"⚙️ Analizando: <i>{file_name}</i>...")
        
        temp_apk = "temp.apk"
        try:
            # 1. Descargar APK
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done: _, done = downloader.next_chunk()
            fh.seek(0)
            with open(temp_apk, "wb") as f: f.write(fh.read())

            # 2. Leer Datos Internos
            apk = APK(temp_apk)
            nombre_limpio = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()
            package_name = apk.package
            
            # 3. Subir Nueva Versión (Primero aseguramos el backup)
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

            # 4. Guardar Nueva Fila en Excel
            sheet.append_row([
                nombre_limpio, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            print(f"✅ Nueva versión registrada en Excel.")

            # 5. LIMPIEZA NUCLEAR (Después de guardar la nueva, borramos las viejas)
            borrados = eliminar_rastros_anteriores(sheet, drive_service, dbx, package_name, file_id)
            
            # 6. Notificación Final
            msj = (
                f"✅ <b>¡Actualización Exitosa!</b>\n"
                f"📦 <b>{nombre_limpio}</b> v{apk.version_name}\n"
                f"🗑️ Versiones viejas eliminadas: {borrados}\n"
                f"🔗 <a href='{link_apk}'>Descargar</a>"
            )
            notificar(msj)

        except Exception as e:
            notificar(f"❌ Error con {file_name}: {e}")
            print(f"❌ Error: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_todo(sheet)
    notificar("🏁 Tienda Sincronizada.")

if __name__ == "__main__":
    main()
