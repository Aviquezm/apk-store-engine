import json
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# --- CONFIGURACIÓN ---
SHEET_ID = "1PcyhKm0lPIVdtXma_3i5VlvzsJnvHfse-qzjDSx4BOo" # Tu ID de Hoja
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def main():
    print("🏭 Generando Catálogo JSON para Neo Store...")
    
    # Conectar a Google Sheets
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    registros = sheet.get_all_records()

    apps_list = []

    # URL base para ver los archivos (Usaremos el enlace al mensaje de Telegram)
    # Formato enlace Telegram: https://t.me/c/CHANNEL_ID/MSG_ID
    # Nota: Para canales privados el ID lleva -100, para enlaces se quita el -100
    
    for r in registros:
        if r['Estado'] == 'Publicado':
            # Limpieza del ID del canal para el enlace (quitar el -100)
            channel_clean_id = str(os.environ['TELEGRAM_CHANNEL_ID']).replace("-100", "")
            msg_link = f"https://t.me/c/{channel_clean_id}/{r['Mensaje_ID']}"

            app_data = {
                "name": r['Nombre'],
                "packageName": r['PackageName'],
                "versionCode": r['VersionCode'],
                "versionName": f"v{r['VersionCode']}", # Asumimos nombre basado en código
                "description": "Actualizado automáticamente desde Drive",
                "downloadUrl": msg_link, # Enlace al mensaje de Telegram
                "icon": "https://cdn-icons-png.flaticon.com/512/873/873107.png" # Icono genérico por ahora
            }
            apps_list.append(app_data)

    # Estructura final del repositorio
    repo_data = {
        "name": "Mi Tienda APK Privada",
        "description": "Repositorio automático",
        "apps": apps_list
    }

    # Guardar archivo
    with open('repo.json', 'w', encoding='utf-8') as f:
        json.dump(repo_data, f, indent=4)
    
    print(f"✅ repo.json generado con {len(apps_list)} aplicaciones.")

if __name__ == "__main__":
    main()
