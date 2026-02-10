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
# 0. UTILIDADES Y NOTIFICACIONES
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
    """Normaliza texto para comparaciones (sin espacios, minúsculas)"""
    if not texto: return ""
    return str(texto).strip().lower().replace('\n', '').replace('\r', '').replace('\t', '')

# ---------------------------------------------------------
# 1. FUNCIÓN DE LIMPIEZA FORENSE (Anti-Duplicados)
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    """Elimina versiones viejas de Drive, Dropbox y Excel."""
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        pkg_nuevo = limpiar_texto(pkg_nuevo_raw)
        
        print(f"\n🔍 [Limpieza] Buscando rastros de: '{pkg_nuevo}'...")
        
        for i, r in enumerate(registros):
            pkg_viejo = limpiar_texto(r.get('Pkg'))
            id_viejo = str(r.get('ID Drive', '')).strip()
            
            # Si coinciden los paquetes Y NO es el mismo archivo que acabamos de subir
            if pkg_viejo == pkg_nuevo and id_viejo != id_archivo_nuevo:
                print(f"   🚨 DUPLICADO ENCONTRADO (Fila {i+2})")
                
                # 1. Borrar de Drive
                try:
                    drive_service.files().delete(fileId=id_viejo).execute()
                    print("      🔥 Drive: Eliminado.")
                except: pass

                # 2. Borrar de Dropbox
                nombre_dbx = f"/{r.get('Nombre', '').replace(' ', '_')}_v{r.get('Version', '')}.apk"
                try:
                    dbx.files_delete_v2(nombre_dbx)
                    print("      🔥 Dropbox: Eliminado.")
                except: pass

                filas_a_borrar.append(i + 2)
                archivos_borrados += 1

        # 3. Borrar filas del Excel (De abajo hacia arriba)
        if filas_a_borrar:
            print(f"🔪 Borrando {len(filas_a_borrar)} filas del Excel...")
            for fila_num in sorted(filas_a_borrar, reverse=True):
                sheet.delete_row(fila_num)
                time.sleep(1.5) # Pausa para no saturar la API
        
        return archivos_borrados
    except Exception as e:
        print(f"❌ Error Limpieza: {e}")
        return 0

# ---------------------------------------------------------
# 2. MOTOR DE EXTRACCIÓN V19 (Jerarquía Estricta)
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1000 
    mejor_data = None
    app_clean = app_name.lower().replace(" ", "")
    
    print(f"\n🕵️‍♂️ [Autopsia] Buscando icono dentro de: {app_name}")
    
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            candidatos = [] 

            for nombre in archivos:
                nombre_lc = nombre.lower()
                
                # 1. FILTRO BÁSICO: Solo imágenes en res/
                if not (nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre):
                    continue
                
                # 2. FILTRO DE BASURA: Ignorar notificaciones y botones
                if 'notification' in nombre_lc or 'abc_' in nombre_lc or 'common_' in nombre_lc: continue
                if 'splash' in nombre_lc or 'background' in nombre_lc: continue 

                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    
                    # 3. FILTROS TÉCNICOS
                    if abs(w - h) > 5: continue # Tiene que ser cuadrado
                    if not (36 <= w <= 1024): continue # Ni muy chico ni gigante
                    
                    # --- SISTEMA DE PUNTUACIÓN V19 ---
                    puntuacion = 0
                    
                    # >> REGLA 1: CASOS ESPECIALES (Truecaller) <<
                    # Nadie le gana a esto.
                    if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 10000
                    if 'tc_' in nombre_lc: puntuacion += 500

                    # >> REGLA 2: ESTÁNDAR DE ANDROID (Song Finder / Shazam) <<
                    # La mayoría de apps (y mods) ponen su icono aquí.
                    # Le damos mucho valor para que gane a cualquier imagen interna.
                    if 'ic_launcher_round' in nombre_lc: puntuacion += 5000
                    if 'ic_launcher' in nombre_lc: puntuacion += 4500
                    if 'app_icon' in nombre_lc: puntuacion += 4000

                    # >> REGLA 3: DESEMPATE POR CALIDAD <<
                    # Si hay varios ic_launcher, queremos el de mejor calidad (xxxhdpi)
                    if 'xxxhdpi' in nombre_lc: puntuacion += 500
                    elif 'xxhdpi' in nombre_lc: puntuacion += 300
                    elif 'xhdpi' in nombre_lc: puntuacion += 200
                    
                    # >> REGLA 4: COINCIDENCIAS MENORES (Baja prioridad) <<
                    # Solo suman un poquito, nunca ganarán al launcher.
                    if 'shazam' in nombre_lc: puntuacion += 100 
                    if app_clean in nombre_lc: puntuacion += 100
                    
                    # Log de candidatos
                    if puntuacion > 0:
                        candidatos.append((nombre, puntuacion))

                    if puntuacion > mejor_puntuacion:
                        mejor_puntuacion = puntuacion
                        mejor_data = data
                        
                except: continue
            
            # REPORTE EN CONSOLA (Para que veas quién ganó)
            candidatos.sort(key=lambda x: x[1], reverse=True)
            if candidatos:
                print(f"   🏆 Ganador: {candidatos[0][0]} ({candidatos[0][1]} pts)")
                if len(candidatos) > 1:
                    print(f"   🥈 Segundo: {candidatos[1][0]} ({candidatos[1][1]} pts)")
            else:
                print("   ⚠️ FALLO: No se encontró ningún icono válido.")
                
        return mejor_data
    except Exception as e:
        print(f"❌ Error crítico en autopsia: {e}")
        return None

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
# 4. MAIN & CONEXIONES
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
    print("🚀 Iniciando Motor V19 (Icono Jerárquico)...")
    
    dbx = conectar_dropbox()
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    # Detección de Nuevos
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
        print(f"\n⚙️ Procesando: {file_name}")
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
            
            # --- EXTRACCIÓN DEL ICONO ---
            icon_data = extraer_icono_precision(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            
            # Subida APK
            nombre_final = f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
            
            # Subida Icono
            link_icon = "https://via.placeholder.com/150" # Icono por defecto si falla
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                url_subida = subir_a_dropbox(dbx, icon_filename, icon_filename)
                if url_subida: link_icon = url_subida
                os.remove(icon_filename)

            # Guardar en Excel
            sheet.append_row([
                nombre_limpio, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            
            # Limpiar versiones viejas
            borrados = eliminar_rastros_anteriores(sheet, drive_service, dbx, apk.package, file_id)
            
            # Reporte final
            msj = (
                f"✅ <b>¡Actualizado!</b>\n"
                f"📦 {nombre_limpio} v{apk.version_name}\n"
                f"🎨 Icono: {'Recuperado' if icon_data else 'Genérico'}\n"
                f"🗑️ Limpieza: {borrados} versiones eliminadas"
            )
            notificar(msj)

        except Exception as e:
            notificar(f"❌ Error con {file_name}: {e}")
            print(f"❌ Error crítico: {e}")
        finally:
            if os.path.exists(temp_apk): os.remove(temp_apk)

    sincronizar_todo(sheet)
    notificar("🏁 Tienda Sincronizada.")

if __name__ == "__main__":
    main()
