import os
import json
import gspread
import io
import asyncio
import subprocess
import re
import zipfile
import shutil
from telethon import TelegramClient
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# --- CONFIGURACIÓN DE SEGURIDAD (SECRETS) ---
ADMIN_ID = int(os.environ['ADMIN_ID'])
DRIVE_FOLDER_ID = os.environ['DRIVE_FOLDER_ID']
SHEET_ID = os.environ['SHEET_ID']

API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
CHANNEL_ID = int(os.environ['TELEGRAM_CHANNEL_ID'])
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

# ---------------------------------------------------------
# 🔍 CAZA ICONO REAL (VERSIÓN DEFINITIVA "SIN CENSURA")
# ---------------------------------------------------------
def cazar_icono_real(apk_path):
    try:
        # 1. Preguntamos a AAPT dónde cree que están los iconos
        cmd = ['aapt', 'dump', 'badging', apk_path]
        out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
        
        # Capturamos todas las rutas que AAPT mencione
        icon_entries = re.findall(r"application-icon-\d+:'([^']+)'", out)
        default_icon = re.search(r"icon='([^']+)'", out)
        if default_icon: icon_entries.append(default_icon.group(1))

        # Listas de prioridad para desempatar
        prioridades = ['xxxhdpi', 'xxhdpi', 'xhdpi', 'hdpi', 'mdpi']
        candidatos = []

        with zipfile.ZipFile(apk_path, 'r') as z:
            nombres = z.namelist()

            # ESTRATEGIA A: Buscamos la ruta EXACTA que dijo AAPT
            # (Validando solo que sea imagen y no XML)
            for icon_path in icon_entries:
                if icon_path in nombres and icon_path.lower().endswith(('.png', '.webp', '.jpg')):
                    return icon_path # ¡Lo encontramos a la primera!

            # ESTRATEGIA B: Búsqueda por coincidencia de nombre
            # Recolectamos nombres base (ej: si dice res/xml/ic_launcher.xml -> buscamos "ic_launcher")
            nombres_base = set()
            for icon_path in icon_entries:
                base = os.path.splitext(os.path.basename(icon_path))[0]
                nombres_base.add(base)
            
            # Si aapt no dijo nada útil, agregamos los nombres estándar
            if not nombres_base:
                nombres_base.update(['ic_launcher', 'icon', 'app_icon'])

            for n in nombres:
                # El archivo debe ser imagen
                if not n.lower().endswith(('.png', '.webp', '.jpg')):
                    continue
                
                # IMPORTANTE: Ya NO filtramos "drawable". Buscamos en todas partes.
                # Solo evitamos basura del sistema
                if 'build-data' in n or 'META-INF' in n:
                    continue

                # Chequeamos si el archivo tiene el nombre que buscamos
                nombre_archivo = os.path.basename(n).split('.')[0]
                if any(base == nombre_archivo for base in nombres_base): # Coincidencia exacta de nombre
                    candidatos.append(n)
                elif any(base in nombre_archivo for base in nombres_base): # Coincidencia parcial
                    candidatos.append(n)

            if not candidatos:
                return None

            # ESTRATEGIA C: Elegir el MEJOR de los candidatos (El más pesado suele ser el mejor)
            def score(path):
                file_size = z.getinfo(path).file_size
                density_bonus = 0
                # Damos puntos extra si está en una carpeta de alta densidad
                for i, p in enumerate(reversed(prioridades)):
                    if p in path:
                        density_bonus = (i + 1) * 10000 
                        break
                return density_bonus + file_size

            candidatos.sort(key=score, reverse=True)
            return candidatos[0]

    except Exception as e:
        print(f"❌ Error cazando icono: {e}")

    return None

# ---------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------
async def main():
    print("🚀 Iniciando Extractor Maestro v9...")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1

    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    # Usamos un set para búsqueda rápida
    procesados = {str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')}

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(q=query, fields="files(id, name)").execute().get('files', [])

    async with client:
        for item in items:
            file_id = item['id']
            file_name = item['name']

            if not file_name.lower().endswith('.apk'): continue
            if file_id in procesados: continue

            await client.send_message(ADMIN_ID, f"🕵️ **Analizando:** `{file_name}`")

            temp_apk = "temp.apk"
            final_icon = "icon_final.png"
            DEFAULT_ICON = "default_icon.png"

            try:
                # 1. DESCARGA APK
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done: _, done = downloader.next_chunk()
                fh.seek(0)
                with open(temp_apk, "wb") as f: f.write(fh.read())

                # 2. INFO BÁSICA
                out = subprocess.run(['aapt', 'dump', 'badging', temp_apk], capture_output=True, text=True, encoding='utf-8', errors='ignore').stdout
                
                pkg_match = re.search(r"package: name='([^']+)'", out)
                pkg = pkg_match.group(1) if pkg_match else "com.unknown"
                
                ver_match = re.search(r"versionCode='([^']+)'", out)
                ver = ver_match.group(1) if ver_match else "1"
                
                label_match = re.search(r"application-label:'([^']+)'", out)
                label = label_match.group(1) if label_match else pkg

                # 3. EXTRACCIÓN DE ICONO (REAL O DEFAULT)
                ruta_icono = cazar_icono_real(temp_apk)
                usa_default = True

                if ruta_icono:
                    try:
                        with zipfile.ZipFile(temp_apk, 'r') as z:
                            with z.open(ruta_icono) as src, open(final_icon, "wb") as trg:
                                trg.write(src.read())
                        usa_default = False
                        await client.send_message(ADMIN_ID, f"🎯 **Icono REAL hallado:** `{ruta_icono}`")
                    except Exception as e:
                        print(f"Error extrayendo ruta hallada: {e}")

                # Si falló la extracción o no encontró nada, usa el default
                if usa_default:
                    if os.path.exists(DEFAULT_ICON):
                        shutil.copyfile(DEFAULT_ICON, final_icon)
                        await client.send_message(ADMIN_ID, f"⚠️ Usando icono default para `{label}` (No se halló PNG interno)")
                    else:
                         await client.send_message(ADMIN_ID, f"❌ ALERTA: No tienes `{DEFAULT_ICON}` en el repo.")

                # 4. SUBIR A TELEGRAM
                icon_msg_id = ""
                # Subimos el icono solo como foto primero (para tener el ID del icono)
                if os.path.exists(final_icon):
                    msg_icon = await client.send_file(CHANNEL_ID, final_icon, caption=f"🖼 Icono: {label}")
                    icon_msg_id = str(msg_icon.id)

                # Subimos la APK (usando el mismo icono como miniatura visual)
                msg_apk = await client.send_file(
                    CHANNEL_ID,
                    temp_apk,
                    caption=f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}",
                    thumb=final_icon if os.path.exists(final_icon) else None
                )

                # 5. GUARDAR EN EXCEL (Orden Exacto: A->H)
                # Nombre | Estado | Notas | Ver | Pkg | MsgID | DriveID | IconoURL
                sheet.append_row([
                    label, 
                    "Publicado", 
                    "Auto", 
                    ver, 
                    pkg, 
                    str(msg_apk.id), 
                    file_id, 
                    icon_msg_id
                ])

                await client.send_message(ADMIN_ID, f"✨ `{label}` listo y publicado.")

            except Exception as e:
                await client.send_message(ADMIN_ID, f"🔥 Error procesando `{file_name}`:\n`{str(e)}`")

            finally:
                if os.path.exists(temp_apk): os.remove(temp_apk)
                if os.path.exists(final_icon): os.remove(final_icon)

if __name__ == "__main__":
    asyncio.run(main())
