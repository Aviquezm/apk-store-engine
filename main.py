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
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]


# ---------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------
def notificar(mensaje):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        )
    except Exception:
        pass


def calcular_hash(file_path):
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(4096), b""):
            sha256.update(block)
    return sha256.hexdigest()


def nombre_seguro(texto):
    return re.sub(r'[^a-zA-Z0-9]', '_', str(texto).strip().lower())


# ---------------------------------------------------------
# RECONCILIACIÓN COMPLETA: DRIVE ↔ SHEETS ↔ DROPBOX
# ---------------------------------------------------------
def reconciliar_todo(sheet, drive_service, dbx):
    """Compara Drive, Sheets y Dropbox. Elimina lo que falte en Drive."""
    print("RECONCILIACION INICIANDO...")

    try:
        # 1. Obtener TODOS los archivos APK en Drive
        query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
        items_drive = (
            drive_service.files()
            .list(q=query, fields="files(id, name)")
            .execute()
            .get('files', [])
        )
        ids_en_drive = {
            item['id'] for item in items_drive
            if item['name'].lower().endswith('.apk')
        }

        print(f"Archivos APK en Drive: {len(ids_en_drive)}")

        # 2. Obtener TODOS los registros en Sheets
        registros = sheet.get_all_records()
        print(f"Registros en Sheets: {len(registros)}")

        # 3. Detectar qué IDs en Sheets YA NO ESTÁN en Drive
        filas_a_eliminar = []
        for idx, r in enumerate(registros):
            id_drive = str(r.get('ID Drive', '')).strip()
            nombre = str(r.get('Nombre', '')).strip()
            version = str(r.get('Version', '')).strip()

            if id_drive and id_drive not in ids_en_drive:
                print(f"DETECTADO ELIMINADO: {nombre} v{version}")
                filas_a_eliminar.append((idx + 2, nombre, version))

        # 4. Eliminar de Dropbox y Sheets lo que ya no está en Drive
        if filas_a_eliminar:
            print(f"Limpiando {len(filas_a_eliminar)} registros huérfanos...")

            for fila_num, nombre, version in sorted(
                filas_a_eliminar, key=lambda x: x[0], reverse=True
            ):
                ruta_dropbox = f"/{nombre}_{version}.apk"
                try:
                    dbx.files_delete_v2(ruta_dropbox)
                    print(f"   Dropbox eliminado: {ruta_dropbox}")
                except Exception as e:
                    print(f"   No se pudo borrar de Dropbox: {e}")

                try:
                    sheet.delete_row(fila_num)
                    print(f"   Fila {fila_num} eliminada de Sheets")
                    time.sleep(1.5)
                except Exception as e:
                    print(f"   Error eliminando fila: {e}")
        else:
            print("No hay registros huérfanos que limpiar")

    except Exception as e:
        print(f"ERROR en reconciliación: {e}")
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------
# SINCRONIZACIÓN DE DROPBOX
# ---------------------------------------------------------
def sincronizar_dropbox(sheet, dbx):
    """Elimina archivos de Dropbox que no están en Sheets"""
    print("Escaneando Dropbox para purgar huérfanos...")
    try:
        registros = sheet.get_all_records()

        archivos_legales = []
        for r in registros:
            if not r.get('Pkg'):
                continue
            nombre = str(r.get('Nombre', '')).strip()
            version = str(r.get('Version', '')).strip()
            ruta_esperada = f"/{nombre}_{version}.apk".lower()
            archivos_legales.append(ruta_esperada)

        resultado = dbx.files_list_folder('')
        for entrada in resultado.entries:
            if isinstance(entrada, dropbox.files.FileMetadata):
                if entrada.path_display.lower() not in archivos_legales:
                    print(f"Eliminando huérfano Dropbox: {entrada.path_display}")
                    try:
                        dbx.files_delete_v2(entrada.path_display)
                    except Exception as e:
                        print(f"   No se pudo borrar: {e}")

    except Exception as e:
        print(f"ERROR en sincronización Dropbox: {e}")


# ---------------------------------------------------------
# GENERADOR HTML Y JSON
# ---------------------------------------------------------
def generar_sistema_completo(sheet):
    print("Generando Sistema completo...")
    registros = sheet.get_all_records()

    obtainium_apps = []
    store_apps = []
    archivos_html_validos = ["index.html"]

    for r in registros:
        if not r.get('Pkg'):
            continue

        nombre = str(r.get('Nombre', 'App')).strip()
        version = str(r.get('Version', '1.0')).strip()
        link_apk = str(r.get('Link APK', '')).strip().replace("dl=0", "dl=1")
        pkg = str(r.get('Pkg', '')).strip()
        icono = str(r.get('Icono', '')).strip()
        version_code = str(r.get('Version Code', '0')).strip()

        filename = f"{nombre_seguro(nombre)}.html"
        archivos_html_validos.append(filename)

        html_content = (
            f"<!DOCTYPE html>\n<html>\n"
            f"<head><title>{nombre}</title></head>\n"
            f"<body>\n<h1>{nombre}</h1>\n"
            f"<p>Version: {version}</p>\n"
            f'<a href="{link_apk}">Descargar {nombre} v{version}</a>\n'
            f"</body>\n</html>"
        )

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

    # Limpiar HTMLs viejos
    try:
        archivos_locales = os.listdir('.')
        for archivo in archivos_locales:
            if archivo.endswith('.html') and archivo not in archivos_html_validos:
                os.remove(archivo)
                print(f"HTML huérfano eliminado: {archivo}")
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

    print(f"JSONs generados: {len(store_apps)} apps")

    with open("index.html", "w", encoding='utf-8') as f:
        f.write(
            f"<html><body><h1>V38 Online - Tienda APK</h1>"
            f"<p>Apps: {len(store_apps)}</p></body></html>"
        )


# ---------------------------------------------------------
# PROCESAR NUEVAS APKs
# ---------------------------------------------------------
def procesar_nuevas_apks(sheet, drive_service, dbx):
    """Descarga APKs nuevas de Drive y las sube a Dropbox/Sheets"""
    registros = sheet.get_all_records()
    procesados = {
        str(r.get('ID Drive', '')).strip()
        for r in registros
        if r.get('ID Drive')
    }

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = (
        drive_service.files()
        .list(q=query, fields="files(id, name)")
        .execute()
        .get('files', [])
    )

    nuevos = [
        i for i in items
        if i['name'].lower().endswith('.apk')
        and str(i['id']).strip() not in procesados
    ]

    if not nuevos:
        print("No hay APKs nuevas para procesar")
        return

    notificar(f"Procesando {len(nuevos)} APKs nuevas")

    for item in nuevos:
        temp_apk = "temp.apk"
        try:
            request = drive_service.files().get_media(fileId=item['id'])
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            fh.seek(0)
            with open(temp_apk, "wb") as f:
                f.write(fh.read())

            apk = APK(temp_apk)
            nombre = re.sub(r'\s*v?\d+.*$', '', apk.application).strip()

            link_icon = f"{REPO_URL_BASE}default_icon.png"

            path = f"/{nombre}_{apk.version_name}.apk"
            with open(temp_apk, "rb") as f:
                dbx.files_upload(f.read(), path, mode=WriteMode('overwrite'))

            l_apk = dbx.sharing_create_shared_link_with_settings(path).url
            link_apk = l_apk.replace("dl=0", "dl=1")

            sheet.append_row([
                nombre,
                "Publicado",
                link_apk,
                apk.version_name,
                apk.package,
                link_icon,
                item['id'],
                "Dropbox",
                str(apk.version_code),
                calcular_hash(temp_apk),
                str(os.path.getsize(temp_apk))
            ])

            print(f"Agregado a Sheets: {nombre} v{apk.version_name}")
            notificar(f"{nombre} v{apk.version_name} listo")
            time.sleep(2)

        except Exception as e:
            print(f"ERROR Procesando {item['name']}: {e}")
            notificar(f"Error: {str(e)[:100]}")
        finally:
            if os.path.exists(temp_apk):
                os.remove(temp_apk)


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def main():
    print("Iniciando Motor con Reconciliación Completa...")

    dbx = dropbox.Dropbox(
        app_key=DBX_KEY,
        app_secret=DBX_SECRET,
        oauth2_refresh_token=DBX_REFRESH_TOKEN
    )

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        SERVICE_ACCOUNT_JSON, SCOPE
    )

    drive_service = build('drive', 'v3', credentials=creds)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1

    try:
        # PASO 1: RECONCILIACIÓN (detectar eliminados de Drive)
        reconciliar_todo(sheet, drive_service, dbx)

        # PASO 2: RECARGAR Sheets
        sheet = client_gs.open_by_key(SHEET_ID).sheet1

        # PASO 3: PROCESAR NUEVAS APKs
        procesar_nuevas_apks(sheet, drive_service, dbx)

        # PASO 4: RECARGAR Sheets
        sheet = client_gs.open_by_key(SHEET_ID).sheet1

        # PASO 5: SINCRONIZAR DROPBOX
        sincronizar_dropbox(sheet, dbx)

        # PASO 6: GENERAR SITIO WEB Y JSONs
        generar_sistema_completo(sheet)

        print("Reconciliación Completa terminada.")

    except Exception as e:
        print(f"ERROR FATAL: {e}")
        import traceback
        traceback.print_exc()
        notificar(f"Error: {str(e)[:150]}")


if __name__ == "__main__":
    main()
