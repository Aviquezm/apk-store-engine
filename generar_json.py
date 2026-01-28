import json
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURACIÓN DESDE SECRETS ---
SHEET_ID = os.environ['SHEET_ID']
TELEGRAM_CHANNEL_ID = os.environ['TELEGRAM_CHANNEL_ID']
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def main():
    print("🏭 Generando Catálogo JSON Seguro...")
    
    # 1. Conexión a Google Sheets
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    registros = sheet.get_all_records()

    apps_list = []
    
    # 2. Limpieza del ID del canal para los enlaces (quitar el -100)
    channel_clean_id = str(TELEGRAM_CHANNEL_ID).replace("-100", "")
    base_telegram_link = f"https://t.me/c/{channel_clean_id}/"
    icon_generico = "https://cdn-icons-png.flaticon.com/512/873/873107.png"

    for r in registros:
        # Verificamos que la fila tenga los datos mínimos necesarios
        if r.get('Estado') == 'Publicado' and r.get('PackageName'):
            
            # Enlace de descarga del APK
            msg_link = base_telegram_link + str(r['Mensaje_ID'])
            
            # Enlace del icono real (si existe el ID en la columna H)
            icon_link = icon_generico
            if r.get('IconoURL'):
                icon_link = base_telegram_link + str(r['IconoURL'])

            app_data = {
                "name": r['Nombre'],
                "packageName": r['PackageName'],
                "versionCode": r['VersionCode'],
                "versionName": f"v{r['VersionCode']}",
                "description": f"ID: {r['PackageName']}",
                "downloadUrl": msg_link,
                "icon": icon_link
            }
            apps_list.append(app_data)

    # 3. Estructura final del repositorio
    repo_data = {
        "name": "Mi Tienda APK Privada",
        "description": "Repositorio automático y seguro",
        "apps": apps_list
    }

    # 4. Guardar archivo público
    with open('repo.json', 'w', encoding='utf-8') as f:
        json.dump(repo_data, f, indent=4)
    
    print(f"✅ repo.json generado con {len(apps_list)} aplicaciones.")

if __name__ == "__main__":
    main()
