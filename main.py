import os
import json
import gspread
import io
import asyncio
import subprocess
import re
import zipfile
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# --- CONFIGURACIÓN DE SEGURIDAD (SECRETS) ---
ADMIN_ID = int(os.environ['ADMIN_ID'])
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']

# Datos de Conexión
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def extraer_todo_con_aapt(file_path):
    """Usa la herramienta oficial de Android para extraer datos e iconos"""
    pkg, ver, label, icon_path = None, None, None, None
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        out = result.stdout
        
        # 1. Datos técnicos (Package y Versión)
        pkg = re.search(r"package: name='([^']+)'", out).group(1)
        ver = int(re.search(r"versionCode='([^']+)'", out).group(1))
        
        # 2. Nombre de la App (Prioriza español)
        label_match = re.search(r"application-label-es:'([^']+)'", out)
        if not label_match: label_match = re.search(r"application-label:'([^']+)'", out)
        label = label_match.group(1) if label_match else pkg

        # 3. Ruta del Icono (Buscamos imágenes reales PNG/WebP/JPG)
        icons = re.findall(r"application-icon-\d+:'([^']+)'", out)
        if not icons:
            icon_std = re.search(r"icon='([^']+)'", out)
            if icon_std: icons.append(icon_std.group(1))
        
        # Filtramos para evitar XMLs (iconos adaptativos) que Telegram no procesa como foto
        icon_path = next((i for i in reversed(icons) if i.lower().endswith(('.png', '.webp', '.jpg'))), None)
        
    except Exception as e:
        print(f"Error en extracción AAPT: {e}")
    return pkg, ver, label, icon_path

async def main():
    print("🚀 Iniciando Motor de Tienda Privada (Seguridad Máxima)...")
    
    # 1. Autenticación Google y Telegram
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    # 2. Cargar historial del Excel
    registros = sheet.get_all_records()
    # Usamos ID_Drive (Columna G) para saber qué NO volver a procesar
    procesados = [str(r['ID_Drive']) for r in registros if 'ID_Drive' in r and str(r['ID_Drive']).strip()]

    # 3. Escanear Carpeta de Google Drive
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        if not items:
            print("📭 Carpeta de Drive vacía.")
            return

        for item in items:
            file_id, file_name = item['id'], item['name']

            # Solo procesar APKs nuevas
            if not file_name.lower().endswith('.apk') or file_id in procesados:
                continue

            await client.send_message(ADMIN_ID, f"🕵️ **Detectado:** `{file_name}`\nProcesando...")
            temp_apk = "procesando.apk"
            temp_icon = "icon_extracto.png"
            
            try:
                # A. Descarga desde Drive
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # B. Análisis técnico profundo
                pkg, ver, label, icon_internal_path = extraer_todo_con_aapt(temp_apk)
                
                if not pkg:
                    await client.send_message(ADMIN_ID, f"❌ Error: No se pudo leer `{file_name}`.")
                    continue

                icon_msg_id = ""
                # C. Extracción y subida de Icono
                if icon_internal_path:
                    try:
                        with zipfile.ZipFile(temp_apk, 'r') as z:
                            with z.open(icon_internal_path) as source, open(temp_icon, "wb") as target:
                                target.write(source.read())
                        
                        # Se envía al canal antes que el APK para que Neo Store lo encuentre
                        msg_foto = await client.send_file(CHANNEL_ID, temp_icon, caption=f"🖼 Icono: {label}")
                        icon_msg_id = str(msg_foto.id)
                    except Exception as e:
                        print(f"No se pudo extraer icono: {e}")

                # D. Gestión de versiones (Borrar lo viejo si existe)
                try:
                    cell = sheet.find(pkg)
                    if cell:
                        fila = cell.row
                        old_data = sheet.row_values(fila)
                        # Borrar mensajes de Telegram (APK e Icono)
                        await client.delete_messages(CHANNEL_ID, [int(old_data[5])]) # Col F (Mensaje_ID)
                        if len(old_data) > 7 and old_data[7]:
                            await client.delete_messages(CHANNEL_ID, [int(old_data[7])]) # Col H (IconoURL)
                        
                        # Borrar de Drive (Opcional, según tu flujo)
                        # drive_service.files().delete(fileId=old_data[6]).execute() # Col G
                        
                        sheet.delete_rows(fila)
                except: pass

                # E. Subir APK al Canal
                caption = f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}"
                msg_apk = await client.send_file(
                    CHANNEL_ID, 
                    temp_apk, 
                    caption=caption, 
                    thumb=temp_icon if os.path.exists(temp_icon) else None
                )

                # F. Registrar en Google Sheets (Orden A-H)
                # Nombre | Estado | Notas | Ver | Pkg | MsgID | DriveID | IconoURL
                sheet.append_row([label, "Publicado", "Auto", ver, pkg, str(msg_apk.id), file_id, icon_msg_id])
                
                await client.send_message(ADMIN_ID, f"✨ **{label}** publicado con éxito en el canal.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"🔥 Error procesando `{file_name}`: {e}")
            
            finally:
                # Limpiar archivos temporales para la siguiente app
                if os.path.exists(temp_apk): os.remove(temp_apk)
                if os.path.exists(temp_icon): os.remove(temp_icon)

if __name__ == "__main__":
    asyncio.run(main())
