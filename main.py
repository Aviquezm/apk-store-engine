import os
import json
import gspread
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io
from androguard.core.bytecodes.apk import APK

# Configuración desde GitHub Secrets
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

# Alcances para Google
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

async def main():
    # 1. Conectar a Google Sheets
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    
    sheet = client_gs.open("Control Tienda APK").sheet1
    filas = sheet.get_all_records()

    # 2. Conectar a Telegram
    async with TelegramClient('bot_session', API_ID, API_HASH) as client:
        for i, fila in enumerate(filas, start=2):
            if fila['Estado'] == 'Pendiente':
                file_id = fila['ID_Archivo_Drive']
                print(f"Procesando: {fila['Nombre']}...")

                # Descargar APK de Drive
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                
                # Extraer info técnica (Package Name y Version)
                fh.seek(0)
                with open("temp.apk", "wb") as f:
                    f.write(fh.read())
                
                apk_info = APK("temp.apk")
                package_name = apk_info.get_package()
                version_code = int(apk_info.get_androidversion_code())

                # LÓGICA DE LIMPIEZA: Buscar si ya existe este paquete en la hoja
                for j, fila_antigua in enumerate(filas, start=2):
                    if fila_antigua['PackageName'] == package_name and fila_antigua['Estado'] == 'Publicado':
                        # Borrar mensaje viejo de Telegram
                        try:
                            await client.delete_messages('@tu_canal', [int(fila_antigua['Mensaje_ID'])])
                        except: pass
                        # Marcar versión vieja como Obsoleta
                        sheet.update_cell(j, 3, 'Obsoleto')

                # Subir a Telegram (Sin límite de 50MB)
                msg = await client.send_file('@tu_canal', "temp.apk", caption=f"✅ {fila['Nombre']}\n📦 {package_name}\n🔢 Versión: {version_code}")

                # Actualizar Hoja de Cálculo
                sheet.update_cell(i, 3, 'Publicado')
                sheet.update_cell(i, 4, version_code)
                sheet.update_cell(i, 5, package_name)
                sheet.update_cell(i, 6, msg.id)
                
                os.remove("temp.apk")
                print(f"¡{fila['Nombre']} subido con éxito!")

import asyncio
asyncio.run(main())
