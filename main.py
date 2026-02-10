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
    """Normaliza texto para comparaciones (sin espacios, minúsculas)"""
    if not texto: return ""
    return str(texto).strip().lower().replace('\n', '').replace('\r', '').replace('\t', '')

# ---------------------------------------------------------
# 1. FUNCIÓN DE LIMPIEZA (Anti-Duplicados)
# ---------------------------------------------------------
def eliminar_rastros_anteriores(sheet, drive_service, dbx, pkg_nuevo_raw, id_archivo_nuevo):
    try:
        registros = sheet.get_all_records()
        filas_a_borrar = []
        archivos_borrados = 0
        pkg_nuevo = limpiar_texto(pkg_nuevo_raw)
        
        print(f"\n🔍 [Limpieza] Buscando rastros de: '{pkg_nuevo}'...")
        
        for i, r in enumerate(registros):
            pkg_viejo = limpiar_texto(r.get('Pkg'))
            id_viejo = str(r.get('ID Drive', '')).strip()
            
            if pkg_viejo == pkg_nuevo and id_viejo != id_archivo_nuevo:
                print(f"   🚨 DUPLICADO (Fila {i+2})")
                try: drive_service.files().delete(fileId=id_viejo).execute()
                except: pass
                
                try: 
                    nombre_dbx = f"/{r.get('Nombre', '').replace(' ', '_')}_v{r.get('Version', '')}.apk"
                    dbx.files_delete_v2(nombre_dbx)
                except: pass

                filas_a_borrar.append(i + 2)
                archivos_borrados += 1

        if filas_a_borrar:
            for fila_num in sorted(filas_a_borrar, reverse=True):
                sheet.delete_row(fila_num)
                time.sleep(1.5)
        
        return archivos_borrados
    except Exception as e:
        print(f"❌ Error Limpieza: {e}")
        return 0

# ---------------------------------------------------------
# 2. MOTOR DE EXTRACCIÓN V22 (Afinado para Dead Pixels)
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1
    mejor_data = None
    
    # Respaldo de Fuerza Bruta (pero filtrado)
    candidatos_fb = [] 
    
    print(f"\n🕵️‍♂️ [Autopsia] Buscando icono para: {app_name}")
    
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            candidatos = [] 

            for nombre in archivos:
                nombre_lc = nombre.lower()
                
                # FILTROS BÁSICOS
                if not (nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre): continue
                if 'notification' in nombre_lc or 'abc_' in nombre_lc: continue
                if 'splash' in nombre_lc or 'background' in nombre_lc: continue 

                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    
                    # FILTRO: CUADRADO EXACTO (Margen 2px)
                    if abs(w - h) > 2: continue 
                    # FILTRO: TAMAÑO (Entre 48 y 1024)
                    if w < 48: continue
                    
                    # --- SISTEMA DE PUNTUACIÓN (Nombres) ---
                    puntuacion = 0
                    
                    # Nivel DIOS (Truecaller)
                    if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 10000
                    
                    # Nivel REY (Estándar)
                    if 'ic_launcher_round' in nombre_lc: puntuacion += 5000
                    if 'ic_launcher' in nombre_lc: puntuacion += 4500
                    if 'app_icon' in nombre_lc: puntuacion += 4000
                    
                    # Nivel ALFIL (Calidad)
                    if 'xxxhdpi' in nombre_lc: puntuacion += 500
                    elif 'xxhdpi' in nombre_lc: puntuacion += 300
                    
                    # --- LÓGICA V22: FUERZA BRUTA DE PRECISIÓN ---
                    # Clasificamos por tamaño para desempatar si no hay nombres
                    prioridad_fb = 0
                    
                    # Rango Ideal (Iconos normales de Android)
                    if 96 <= w <= 192: prioridad_fb = 3 
                    # Rango Alto (Iconos HD)
                    elif 193 <= w <= 512: prioridad_fb = 2
                    # Rango Riesgo (Posibles fondos)
                    elif w > 512: prioridad_fb = 1
                    
                    # Guardamos candidato de fuerza bruta
                    candidatos_fb.append((nombre, prioridad_fb, w, data))

                    # Si tiene puntos (coincidió con nombre), es candidato directo
                    if puntuacion > 0:
                        if puntuacion > mejor_puntuacion:
                            mejor_puntuacion = puntuacion
                            mejor_data = data
                            candidatos.append((nombre, puntuacion))
                            
                except: continue
            
            # --- FASE FINAL: EL JUICIO ---
            
            # 1. Si encontramos un nombre oficial, ganamos.
            if mejor_puntuacion > 1000:
                print(f"   🏆 Ganador por Nombre: {candidatos[-1][0]} ({mejor_puntuacion} pts)")
                return mejor_data
            
            # 2. SI NO: Usamos FUERZA BRUTA INTELIGENTE
            print("   ⚠️ No se hallaron nombres oficiales. Activando FUERZA BRUTA V22.")
            
            if candidatos_fb:
                # Ordenamos:
                # 1. Prioridad (Preferimos 96-192px antes que gigantes)
                # 2. Tamaño (Dentro de la misma prioridad, el más grande)
                candidatos_fb.sort(key=lambda x: (x[1], x[2]), reverse=True)
                
                ganador_fb = candidatos_fb[0]
                print(f"   🦍 Ganador Fuerza Bruta: {ganador_fb[0]} (Prioridad: {ganador_fb[1]}, Tamaño: {ganador_fb[2]}px)")
                return ganador_fb[3]
            else:
                print("   ❌ FALLO TOTAL: No hay imágenes válidas.")
                return None

    except Exception as e:
        print(f"❌ Error crítico: {e}")
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
    print("🚀 Iniciando Motor V22 (Afinado)...")
    
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
            
            # EXTRACCIÓN
            icon_data = extraer_icono_precision(temp_apk, apk.application)
            icon_filename = f"icon_{apk.package}.png"
            
            # SUBIDAS
            nombre_final = f"{nombre_limpio.replace(' ', '_')}_v{apk.version_name}.apk"
            link_apk = subir_a_dropbox(dbx, temp_apk, nombre_final)
            
            link_icon = "https://via.placeholder.com/150" 
            if icon_data:
                with open(icon_filename, "wb") as f: f.write(icon_data)
                url_subida = subir_a_dropbox(dbx, icon_filename, icon_filename)
                if url_subida: link_icon = url_subida
                os.remove(icon_filename)

            # EXCEL Y LIMPIEZA
            sheet.append_row([
                nombre_limpio, "Publicado", link_apk, apk.version_name, 
                apk.package, link_icon, file_id, "Dropbox/Repo", str(apk.version_code)
            ])
            borrados = eliminar_rastros_anteriores(sheet, drive_service, dbx, apk.package, file_id)
            
            msj = (
                f"✅ <b>¡Actualizado!</b>\n"
                f"📦 {nombre_limpio} v{apk.version_name}\n"
                f"🎨 Icono: {'Recuperado' if icon_data else 'Genérico'}\n"
                f"🗑️ Limpieza: {borrados}"
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
