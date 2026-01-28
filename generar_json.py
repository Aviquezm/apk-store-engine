import json
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo"
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def main():
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID).sheet1
    registros = sheet.get_all_records()

    apps = []
    # ID del canal limpio (sin -100) para crear enlaces
    chan_id = str(os.environ['TELEGRAM_CHANNEL_ID']).replace("-100", "")
    base_url = f"https://t.me/c/{chan_id}/"

    for r in registros:
        if r['Estado'] == 'Publicado':
            icon_url = base_url + str(r['IconoURL']) if r['IconoURL'] else "https://cdn-icons-png.flaticon.com/512/873/873107.png"
            
            app = {
                "name": r['Nombre'],
                "packageName": r['PackageName'],
                "versionCode": r['VersionCode'],
                "versionName": f"v{r['VersionCode']}",
                "description": "Actualizado via Telegram Bot",
                "downloadUrl": base_url + str(r['Mensaje_ID']),
                "icon": icon_url
            }
            apps.append(app)

    repo = {
        "name": "Mi Tienda Privada",
        "description": "Repositorio Automático",
        "apps": apps
    }

    with open('repo.json', 'w', encoding='utf-8') as f:
        json.dump(repo, f, indent=4)
    print("✅ repo.json generado.")

if __name__ == "__main__":
    main()
