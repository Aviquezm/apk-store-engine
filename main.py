# ---------------------------------------------------------
# MOTOR DE EXTRACCIÓN V19 (Jerarquía Estricta)
# ---------------------------------------------------------
def extraer_icono_precision(apk_path, app_name):
    mejor_puntuacion = -1000 
    mejor_data = None
    app_clean = app_name.lower().replace(" ", "")
    
    print(f"\n🕵️‍♂️ [Autopsia] Buscando icono para: {app_name}")
    
    try:
        with zipfile.ZipFile(apk_path, 'r') as z:
            archivos = z.namelist()
            candidatos = [] 

            for nombre in archivos:
                nombre_lc = nombre.lower()
                
                # 1. FILTRO DE ARCHIVOS
                if not (nombre_lc.endswith(('.png', '.webp')) and 'res/' in nombre): continue
                
                # 2. FILTRO DE BASURA (Importante para no agarrar botones)
                if 'notification' in nombre_lc or 'abc_' in nombre_lc or 'common_' in nombre_lc: continue
                if 'splash' in nombre_lc or 'background' in nombre_lc: continue 

                try:
                    data = z.read(nombre)
                    img = Image.open(io.BytesIO(data))
                    w, h = img.size
                    
                    # 3. FILTROS TÉCNICOS
                    if abs(w - h) > 5: continue # Tiene que ser cuadrado
                    if not (36 <= w <= 1024): continue # Ni muy chico ni gigante
                    
                    # --- SISTEMA DE PUNTUACIÓN V19 ---
                    puntuacion = 0
                    
                    # >> REGLA 1: CASOS ESPECIALES (Truecaller) <<
                    # Nadie le gana a esto.
                    if 'rounded_logo' in nombre_lc or 'tc_logo' in nombre_lc: puntuacion += 10000
                    if 'tc_' in nombre_lc: puntuacion += 500

                    # >> REGLA 2: ESTÁNDAR DE ANDROID (Song Finder / Shazam) <<
                    # La mayoría de apps (y mods) ponen su icono aquí.
                    # Le damos mucho valor para que gane a cualquier imagen interna.
                    if 'ic_launcher_round' in nombre_lc: puntuacion += 5000
                    if 'ic_launcher' in nombre_lc: puntuacion += 4500
                    if 'app_icon' in nombre_lc: puntuacion += 4000

                    # >> REGLA 3: DESEMPATE POR CALIDAD <<
                    # Si hay varios ic_launcher, queremos el de mejor calidad (xxxhdpi)
                    if 'xxxhdpi' in nombre_lc: puntuacion += 500
                    elif 'xxhdpi' in nombre_lc: puntuacion += 300
                    elif 'xhdpi' in nombre_lc: puntuacion += 200
                    
                    # >> REGLA 4: COINCIDENCIAS MENORES (Baja prioridad) <<
                    # Solo suman un poquito, nunca ganarán al launcher.
                    if 'shazam' in nombre_lc: puntuacion += 100 
                    if app_clean in nombre_lc: puntuacion += 100
                    
                    # Log de candidatos
                    if puntuacion > 0:
                        candidatos.append((nombre, puntuacion))

                    if puntuacion > mejor_puntuacion:
                        mejor_puntuacion = puntuacion
                        mejor_data = data
                        
                except: continue
            
            # REPORTE EN CONSOLA (Para que veas quién ganó)
            candidatos.sort(key=lambda x: x[1], reverse=True)
            if candidatos:
                print(f"   🏆 Ganador: {candidatos[0][0]} ({candidatos[0][1]} pts)")
                if len(candidatos) > 1:
                    print(f"   🥈 Segundo: {candidatos[1][0]} ({candidatos[1][1]} pts)")
            else:
                print("   ⚠️ FALLO: No se encontró ningún icono válido.")
                
        return mejor_data
    except Exception as e:
        print(f"❌ Error crítico en autopsia: {e}")
        return None
