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

# --- CONFIGURACIÓN ---
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
# 🔍 FUNCIÓN DEFINITIVA PARA EXTRAER ICONO REAL
# ---------------------------------------------------------
def cazar_icono_real(apk_path):
    try:
        out = subprocess.run(
            ['aapt', 'dump', 'badging', apk_path],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        ).stdout

        icon_xml_match = re.search(
            r"application-icon-\d+:'([^']+\.xml)'", out
        )

        prioridades = ['xxxhdpi', 'xxhdpi', 'xhdpi', 'hdpi', 'mdpi']

        with zipfile.ZipFile(apk_path, 'r') as z:
            names = z.namelist()

            # ===============================
            # 1️⃣ ADAPTIVE ICON (XML)
            # ===============================
            if icon_xml_match:
                xml_path = icon_xml_match.group(1)

                if xml_path in names:
                    xml_data = z.read(xml_path).decode(errors='ignore')

                    drawables = re.findall(
                        r'@(?:mipmap|drawable)/([a-zA-Z0-9_]+)',
                        xml_data
                    )

                    candidatos = []
                    for d in drawables:
                        for n in names:
                            if (
                                d in n
                                and n.lower().endswith('.png')
                                and 'mipmap' in n.lower()
                            ):
                                candidatos.append(n)

                    if candidatos:
                        candidatos.sort(
                            key=lambda x: z.getinfo(x).file_size,
                            reverse=True
                        )
                        return candidatos[0]

            # ===============================
            # 2️⃣ ICONOS CLÁSICOS
            # ===============================
            icon_entries = re.findall(
                r"application-icon-\d+:'([^']+)'", out
            )

            candidatos = []
            for icon in icon_entries:
                base = os.path.splitext(os.path.basename(icon))[0]
                for n in names:
                    if (
                        base in n
                        and n.lower().endswith('.png')
                        and 'mipmap' in n.lower()
                    ):
                        candidatos.append(n)

            if candidatos:
                candidatos.sort(
                    key=lambda x: z.getinfo(x).file_size,
                    reverse=True
                )
                return candidatos[0]

            # ===============================
            # 3️⃣ FALLBACK REAL (último recurso)
            # ===============================
            pngs = [
                n for n in names
                if n.lower().endswith('.png')
                and 'mipmap' in n.lower()
            ]

            if pngs:
                pngs.sort(
                    key=lambda x: z.getinfo(x).file_size,
                    reverse=True
                )
                return pngs[0]

    except Exception as e:
        print(f"❌ Error extrayendo icono: {e}")

    return None

# ---------------------------------------------------------
# 🚀 MAIN
# ---------------------------------------------------------
async def main():
    print("🚀 Iniciando Extractor Atómico v9...")

    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        SERVICE_ACCOUNT_JSON, SCOPE
    )
    client_gs = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1

    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    registros = sheet.get_all_records()
    procesados = {str(r.get('ID_Drive')) for r in registros if r.get('ID_Drive')}

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed=false"
    items = drive_service.files().list(
        q=query,
        fields="files(id, name)"
    ).execute().get('files', [])

    async with client:
        for item in items:
            file_id = item['id']
            file_name = item['name']

            if not file_name.lower().endswith('.apk'):
                continue
            if file_id in procesados:
                continue

            await client.send_message(
                ADMIN_ID,
                f"🕵️ Analizando `{file_name}`"
            )

            temp_apk = "temp.apk"
            final_icon = "icon_final.png"
            DEFAULT_ICON = "default_icon.png"

            try:
                # 📥 Descargar APK
                request = drive_service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)

                done = False
                while not done:
                    _, done = downloader.next_chunk()

                fh.seek(0)
                with open(temp_apk, "wb") as f:
                    f.write(fh.read())

                # 📦 Info APK
                out = subprocess.run(
                    ['aapt', 'dump', 'badging', temp_apk],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='ignore'
                ).stdout

                pkg = re.search(r"package: name='([^']+)'", out).group(1)
                ver = re.search(r"versionCode='([^']+)'", out).group(1)
                label_m = re.search(r"application-label:'([^']+)'", out)
                label = label_m.group(1) if label_m else pkg

                # 🎯 ICONO
                ruta_icono = cazar_icono_real(temp_apk)
                usa_default = True

                if ruta_icono:
                    with zipfile.ZipFile(temp_apk, 'r') as z:
                        with z.open(ruta_icono) as src, open(final_icon, "wb") as trg:
                            trg.write(src.read())
                    usa_default = False
                    await client.send_message(
                        ADMIN_ID,
                        f"🎯 Icono real: `{ruta_icono}`"
                    )

                if usa_default and os.path.exists(DEFAULT_ICON):
                    shutil.copyfile(DEFAULT_ICON, final_icon)
                    await client.send_message(
                        ADMIN_ID,
                        f"⚠️ Usando icono default"
                    )

                # 📤 Subir a Telegram
                icon_msg_id = ""
                if os.path.exists(final_icon):
                    msg_icon = await client.send_file(
                        CHANNEL_ID,
                        final_icon,
                        caption=f"🖼 {label}"
                    )
                    icon_msg_id = str(msg_icon.id)

                msg_apk = await client.send_file(
                    CHANNEL_ID,
                    temp_apk,
                    caption=f"✅ **{label}**\n📦 `{pkg}`\n🔢 v{ver}",
                    thumb=final_icon if os.path.exists(final_icon) else None
                )

                # 📊 Sheet
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

                await client.send_message(
                    ADMIN_ID,
                    f"✨ `{label}` publicado correctamente"
                )

            except Exception as e:
                await client.send_message(
                    ADMIN_ID,
                    f"🔥 Error: `{e}`"
                )

            finally:
                if os.path.exists(temp_apk):
                    os.remove(temp_apk)
                if os.path.exists(final_icon):
                    os.remove(final_icon)

# ---------------------------------------------------------
# ▶️ RUN
# ---------------------------------------------------------
if __name__ == "__main__":
    asyncio.run(main())
