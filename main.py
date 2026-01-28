import os
import json
import gspread
import io
import asyncio
import subprocess
import re
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from androguard.core.bytecodes.apk import APK

# --- TUS DATOS FIJOS ---
ADMIN_ID = 761087529  # TU ID PERSONAL (Aquí llegarán los logs)
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

# Configuración de Entorno
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# --- FUNCIÓN "MODO RUDO" PARA LEER APKs REBELDES ---
def get_apk_data_robust(file_path):
    """Intenta con Androguard, si falla usa AAPT (fuerza bruta)"""
    try:
        # Intento 1: Método Suave (Androguard)
        apk = APK(file_path)
        return apk.get_package(), int(apk.get_androidversion_code()), apk.get_app_name()
    except Exception as e:
        print(f"⚠️ Androguard falló ({e}). Activando AAPT...")
        try:
            # Intento 2: Método Rudo (AAPT)
            cmd = ['aapt', 'dump', 'badging', file_path]
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            output = result.stdout
            
            # Buscamos los datos con expresiones regulares (Regex)
            package = re.search(r"package: name='([^']+)'", output).group(1)
            version_code = int(re.search(r"versionCode='([^']+)'", output).group(1))
            
            # Intentamos buscar el nombre, si no, usamos el package
            label_match = re.search(r"application-label:'([^']+)'", output)
            label = label_match.group(1) if label_match else package
            
            return package, version_code, label
        except Exception as e2:
            print(f"❌ AAPT también falló: {e2}")
            raise Exception("APK corrupto o ilegible por ambos métodos.")

async def main():
    print("🌍 Iniciando sistemas...")
    
    # 1. Conexiones
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    # 2. Cargar Inventario
    registros = sheet.get_all_records()
    procesados = [str(r['ID_Archivo_Drive']) for r in registros if str(r['ID_Archivo_Drive']).strip() != '']

    # 3. Escanear Drive
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        # Mensaje de inicio al ADMIN (Solo a ti)
        await client.send_message(ADMIN_ID, "🤖 **Bot Activo:** Escaneando carpeta de Drive...")

        if not items:
            print("📭 Carpeta vacía.")
            return

        for item in items:
            file_id = item['id']
            file_name = item['name']

            if not file_name.lower().endswith('.apk'):
                continue

            if file_id in procesados:
                continue

            # --- NUEVO APK ENCONTRADO ---
            # Aviso privado a TI
            status_msg = await client.send_message(ADMIN_ID, f"⚙️ **Procesando:** `{file_name}`\n📥 Descargando...")
            
            try:
                # Descarga
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                fh.seek(0)
                with open("temp.apk", "wb") as f:
                    f.write(fh.read())

                # Análisis (Con blindaje anti-errores)
                package_name, version_code, app_label = get_apk_data_robust("temp.apk")
                
                # Aviso de actualización
                await client.edit_message(ADMIN_ID, status_msg.id, f"🔍 Analizado: `{app_label}`\n📦 Package: `{package_name}`\n♻️ Buscando versiones viejas...")

                # Auto-Limpieza
                cell = sheet.find(package_name)
                if cell:
                    fila_vieja = cell.row
                    datos_viejos = sheet.row_values(fila_vieja)
                    try:
                        # Indices aproximados (ajustar si tu excel cambia)
                        # Asumimos que MSG_ID está en col 6 (F) y DRIVE_ID en col 7 (G)
                        msg_id_viejo = int(datos_viejos[5]) 
                        drive_id_viejo = datos_viejos[6]
                        
                        # Borrar del canal
                        await client.delete_messages(CHANNEL_ID, [msg_id_viejo])
                        # Borrar de Drive
                        drive_service.files().delete(fileId=drive_id_viejo).execute()
                        # Borrar de Excel
                        sheet.delete_rows(fila_vieja)
                        await client.send_message(ADMIN_ID, f"🗑️ Versión anterior eliminada.")
                    except Exception as e:
                        print(f"Nota limpieza: {e}")

                # Subida al Canal Público
                caption = f"✅ **{app_label}**\n📦 `{package_name}`\n🔢 Versión: {version_code}"
                msg_final = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, parse_mode='md')

                # Guardar en Excel
                nueva_fila = [app_label, "Publicado", "Auto", version_code, package_name, msg_final.id, file_id]
                sheet.append_row(nueva_fila)

                # Reporte Final a TI
                await client.edit_message(ADMIN_ID, status_msg.id, f"✅ **¡Éxito!**\n`{file_name}` publicado en el canal.")

            except Exception as e:
                error_txt = f"❌ Error con `{file_name}`: {str(e)}"
                print(error_txt)
                await client.send_message(ADMIN_ID, error_txt)
            
            finally:
                if os.path.exists("temp.apk"):
                    os.remove("temp.apk")

if __name__ == "__main__":
    asyncio.run(main())
