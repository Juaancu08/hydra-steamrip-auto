import requests
from bs4 import BeautifulSoup
import json
import time
from urllib.parse import urljoin

# URL base de SteamRIP
BASE_URL = "https://steamrip.com/"
# URL de la fuente original que usas en Hydra (para no perder lo que ya tienes)
# Puedes cambiar esto si usas otra fuente
SOURCE_URL = "https://hydralinks.cloud/sources/steamrip.json"

def obtener_enlaces_de_juego(url_juego):
    """
    Entra en la página de un juego y busca los enlaces de descarga.
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url_juego, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        enlaces = []
        
        # --- ZONA DE AJUSTE ---
        # Buscamos enlaces que contengan "magnet:" o que sean de descarga directa.
        # Ajusta los selectores si es necesario. A menudo están en botones o divs con clases como 'download'.
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            texto = a_tag.get_text(strip=True).lower()
            
            # Criterio: si es un magnet, o si el texto/botón indica descarga y no es un enlace social.
            if "magnet:" in href:
                enlaces.append(href)
            elif ("download" in texto or "descargar" in texto) and "steamrip.com" not in href and "http" in href:
                # Asegurarnos de que no sea un enlace interno de la web
                enlaces.append(href)
        # --- FIN ZONA DE AJUSTE ---
        
        # Devolvemos una lista única de enlaces
        return list(set(enlaces))
        
    except Exception as e:
        print(f"Error obteniendo enlaces de {url_juego}: {e}")
        return []

def scrape_steamrip():
    """
    Función principal que recorre la web y genera el JSON.
    """
    juegos_data = []
    
    # 1. Cargamos los datos existentes de la fuente original para no perder nada si falla algo.
    try:
        print("Cargando datos base desde HydraLinks...")
        resp = requests.get(SOURCE_URL, timeout=15)
        if resp.status_code == 200:
            data_original = resp.json()
            # Hydra espera un objeto con "name" y "downloads"
            if isinstance(data_original, dict) and "downloads" in data_original:
                juegos_data = data_original["downloads"]
                print(f"Cargados {len(juegos_data)} juegos existentes.")
    except Exception as e:
        print(f"No se pudieron cargar los datos base: {e}. Empezaremos de cero.")

    # Mapa para acceso rápido por título
    mapa_juegos = {juego["title"]: juego for juego in juegos_data}

    # 2. Buscamos la lista de juegos en la página principal (o una página de "All Games")
    # Esto es un ejemplo. SteamRIP puede tener paginación.
    # Buscamos enlaces que parezcan ser de juegos.
    try:
        print("Buscando lista de juegos en la página principal...")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(BASE_URL, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # --- ZONA DE AJUSTE ---
        # Buscamos enlaces que contengan "/free-download/" o similar.
        enlaces_juegos = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            # Heurística: si la URL parece una página de juego.
            if "free-download" in href or "/game/" in href or (href.count('/') == 2 and "steamrip.com" not in href):
                url_completa = urljoin(BASE_URL, href)
                enlaces_juegos.append(url_completa)
        # --- FIN ZONA DE AJUSTE ---

        enlaces_juegos = list(set(enlaces_juegos)) # Únicos
        print(f"Encontrados {len(enlaces_juegos)} posibles juegos para procesar.")

        # 3. Procesamos cada juego (limitamos a unos 10 para no tardar demasiado en la primera prueba)
        for i, url_juego in enumerate(enlaces_juegos[:15]): # Empezamos con 15 para probar
            print(f"Procesando {i+1}/{len(enlaces_juegos[:15])}: {url_juego}")
            
            # Extraer título de la URL (mejor que del HTML para robustez)
            titulo = url_juego.strip('/').split('/')[-1].replace('-free-download', '').replace('-', ' ').title()
            
            # Obtener los enlaces nuevos
            nuevos_enlaces = obtener_enlaces_de_juego(url_juego)
            
            if not nuevos_enlaces:
                print(f"  No se encontraron enlaces nuevos para {titulo}")
                continue

            # Actualizar o crear la entrada en el JSON
            if titulo in mapa_juegos:
                # Solo actualizamos si los enlaces son diferentes para no crear commits innecesarios
                if set(mapa_juegos[titulo].get("uris", [])) != set(nuevos_enlaces):
                    print(f"  Actualizando {titulo} con nuevos enlaces.")
                    mapa_juegos[titulo]["uris"] = nuevos_enlaces
                else:
                    print(f"  {titulo} ya está actualizado.")
            else:
                print(f"  Añadiendo nuevo juego: {titulo}")
                mapa_juegos[titulo] = {
                    "title": titulo,
                    "uris": nuevos_enlaces,
                    "uploadDate": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "fileSize": "Desconocido", # Podrías intentar extraerlo, pero es complejo
                    "repacker": "SteamRIP"
                }
            
            time.sleep(1) # Pequeña pausa para no saturar el servidor

    except Exception as e:
        print(f"Error en el scraping principal: {e}")

    # 4. Construir el JSON final en el formato de Hydra
    # El formato de Hydra es un objeto con "name" y "downloads".
    # HydraLinks a veces usa solo el array, a veces el objeto. Usaremos el objeto.
    output = {
        "name": "SteamRIP (Auto)",
        "downloads": list(mapa_juegos.values())
    }
    
    # 5. Guardar en el archivo
    with open('steamrip_auto.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"Proceso completado. Se han procesado {len(mapa_juegos)} juegos en total.")

if __name__ == "__main__":
    scrape_steamrip()
