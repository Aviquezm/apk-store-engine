import json
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_ID = os.environ['SHEET_ID']
SERVICE_ACCOUNT_JSON = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def main():
    print("🏭 Generando Catálogo Nativo para Obtainium...")
    
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_JSON, SCOPE)
    client_gs = gspread.authorize(creds)
    sheet = client_gs.open_by_key(SHEET_ID).sheet1
    registros = sheet.get_all_records()

    apps_list = []

    for r in registros:
        # En main.py guardamos el Link de la APK en la columna 'Notas' (que en el JSON del sheet suele ser la clave 'Notas')
        # Y el Link del Icono en 'Mensaje_ID'
        
        apk_url = r.get('Notas') 
        icon_url = r.get('Mensaje_ID')

        # Verificamos que sea un link de dropbox válido
        if apk_url and 'dropbox.com' in str(apk_url):
            
            app_data = {
                "id": r['PackageName'],
                "name": r['Nombre'],
                "version": f"v{r['VersionCode']}",
                "url": apk_url, # Enlace directo dl=1
                "icon": icon_url, # Icono directo dl=1
                "pinned": False,
                "categories": [],
                "env": {},
                "provider": "HTML", # HTML/File Provider funciona bien con enlaces directos
                "about": "Descarga directa desde Dropbox Personal"
            }
            apps_list.append(app_data)

    with open('obtainium.json', 'w', encoding='utf-8') as f:
        json.dump(apps_list, f, indent=4)
    
    print(f"✅ obtainium.json generado con {len(apps_list)} apps.")

if __name__ == "__main__":
    main()
