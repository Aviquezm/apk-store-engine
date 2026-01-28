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
from androguard.core.bytecodes.apk import APK

# --- DATOS FIJOS ---
ADMIN_ID = 761087529
DRIVE_FOLDER_ID = "1Pyst-T_TTycEl2R1vvtfu_cs1_WKHCaB"
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"

# Configuración
API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_icon_path_aapt(file_path):
    """Busca la ruta del icono usando fuerza bruta (AAPT)"""
    try:
        cmd = ['aapt', 'dump', 'badging', file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        output = result.stdout
        
        # 1. Busca el icono de alta calidad
        icons = re.findall(r"application-icon-\d+:'([^']+)'", output)
        if icons: return icons[-1] # Retorna el último (mejor calidad)
        
        # 2. Busca icono estándar
        icon_std = re.search(r"icon='([^']+)'", output)
        if icon_std: return icon_std.group(1)
        
        return None
    except Exception as e:
        print(f"Error AAPT: {e}")
        return None

def get_basic_info(file_path):
    """Intenta sacar Package y Version sea como sea"""
    pkg, ver, name = None, None, None
    
    # Intento 1: Androguard
    try:
        apk = APK(file_path)
        pkg = apk.get_package()
        ver = int(apk.get_androidversion_code())
        name = apk.get_app_name()
    except: pass

    # Intento 2: AAPT
    if not pkg or not ver:
        try:
            cmd = ['aapt', 'dump', 'badging', file_path]
            res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
            out = res.stdout
            if not pkg: pkg = re.search(r"package: name='([^']+)'", out).group(1)
            if not ver: ver = int(re.search(r"versionCode='([^']+)'", out).group(1))
            if not name: 
                match = re.search(r"application-label:'([^']+)'", out)
                name = match.group(1) if match else pkg
        except: pass
        
    return pkg, ver, name

async def main():
    print("🌍 Iniciando Modo Detective...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    # Cargar procesados (Miramos Columna G - ID_Drive)
    registros = sheet.get_all_records()
    procesados = []
    for r in registros:
        # Aseguramos que existe la columna y no está vacía
        if 'ID_Drive' in r and str(r['ID_Drive']).strip():
            procesados.append(str(r['ID_Drive']))

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    items = results.get('files', [])

    async with client:
        if not items: return

        for item in items:
            file_id = item['id']
            file_name = item['name']

            if not file_name.lower().endswith('.apk') or file_id in procesados:
                continue

            # --- NUEVO APK DETECTADO ---
            log_msg = await client.send_message(ADMIN_ID, f"🕵️ **Analizando:** `{file_name}`")
            extracted_icon = "temp_icon.png"
            
            try:
                # 1. Descargar
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open("temp.apk", "wb") as f: f.write(fh.read())

                # 2. Info Básica
                pkg, ver, label = get_basic_info("temp.apk")
                if not pkg: raise Exception("No se pudo leer el PackageName (APK corrupto?)")

                # 3. ICONO (La parte crítica)
                icon_path = get_icon_path_aapt("temp.apk")
                icon_msg_id = ""
                
                if icon_path:
                    try:
                        # Intentamos extraer del ZIP
                        with zipfile.ZipFile("temp.apk", 'r') as z:
                            # A veces la ruta empieza con res/ y en el zip no... probamos variantes
                            nombres_zip = z.namelist()
                            if icon_path in nombres_zip:
                                with z.open(icon_path) as s, open(extracted_icon, "wb") as t:
                                    t.write(s.read())
                            else:
                                await client.send_message(ADMIN_ID, f"⚠️ Ruta icono `{icon_path}` no hallada en ZIP.")
                                # Intento desesperado: buscar cualquier .png que se llame ic_launcher
                                possible = [n for n in nombres_zip if 'ic_launcher' in n and n.endswith('.png')]
                                if possible:
                                    with z.open(possible[-1]) as s, open(extracted_icon, "wb") as t:
                                        t.write(s.read())
                        
                        # Si logramos crear la imagen, la subimos
                        if os.path.exists(extracted_icon):
                            msg_icon = await client.send_file(CHANNEL_ID, extracted_icon, caption=f"Icono: {label}")
                            icon_msg_id = msg_icon.id
                            await client.send_message(ADMIN_ID, "✅ Icono extraído y subido.")
                    except Exception as e_icon:
                        await client.send_message(ADMIN_ID, f"❌ Error extrayendo icono: {str(e_icon)}")
                else:
                    await client.send_message(ADMIN_ID, "⚠️ AAPT no encontró ninguna ruta de icono en el código.")

                # 4. Limpieza Versión Vieja
                try:
                    cell = sheet.find(pkg)
                    if cell:
                        fila = cell.row
                        old = sheet.row_values(fila)
                        # Asumiendo orden: Nom, Est, Not, Ver, Pkg, MsgID, DriveID, IconURL
                        if len(old) > 5: await client.delete_messages(CHANNEL_ID, [int(old[5])])
                        if len(old) > 7 and old[7]: await client.delete_messages(CHANNEL_ID, [int(old[7])])
                        if len(old) > 6: drive_service.files().delete(fileId=old[6]).execute()
                        sheet.delete_rows(fila)
                except: pass

                # 5. Subir APK
                caption = f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}"
                thumb = extracted_icon if os.path.exists(extracted_icon) else None
                msg_apk = await client.send_file(CHANNEL_ID, "temp.apk", caption=caption, thumb=thumb)

                # 6. Guardar (Importante: IconoURL al final)
                # Col: A|B|C|D|E|F|G|H
                new_row = [label, "Publicado", "Auto", ver, pkg, msg_apk.id, file_id, icon_msg_id]
                sheet.append_row(new_row)

                await client.edit_message(ADMIN_ID, log_msg.id, f"✅ **Listo:** `{label}` publicado.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"🔥 Error fatal con {file_name}: {e}")
            
            finally:
                if os.path.exists("temp.apk"): os.remove("temp.apk")
                if os.path.exists(extracted_icon): os.remove(extracted_icon)

if __name__ == "__main__":
    asyncio.run(main())
