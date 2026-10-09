import requests
from bs4 import BeautifulSoup
import json
import time
from urllib.parse import urljoin

BASE_URL = "https://steamrip.com/"
SOURCE_URL = "https://hydralinks.cloud/sources/steamrip.json"

def obtener_enlaces_de_juego(url_juego):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = requests.get(url_juego, headers=headers, timeout=20)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        enlaces = []
        titulo_real = None
        
        h1 = soup.find('h1')
        if h1:
            titulo_real = h1.get_text(strip=True)
        
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            clases = a_tag.get('class', [])
            
            if href.startswith('//'):
                href = 'https:' + href
            elif href.startswith('/'):
                href = urljoin(BASE_URL, href)
            
            es_boton = 'shortc-button' in clases
            es_externo = 'steamrip.com' not in href
            
            if es_boton and es_externo:
                enlaces.append(href)
        
        return titulo_real, list(set(enlaces))
        
    except Exception as e:
        print(f"  Error: {e}")
        return None, []

def scrape_steamrip():
    juegos_data = []
    
    try:
        print("Cargando datos base desde HydraLinks...")
        resp = requests.get(SOURCE_URL, timeout=15)
        if resp.status_code == 200:
            data_original = resp.json()
            if isinstance(data_original, dict) and "downloads" in data_original:
                juegos_data = data_original["downloads"]
                print(f"Cargados {len(juegos_data)} juegos existentes.")
    except Exception as e:
        print(f"No se pudieron cargar los datos base: {e}")

    mapa_juegos = {juego["title"]: juego for juego in juegos_data}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    try:
        print("Buscando lista de juegos en la página principal...")
        response = requests.get(BASE_URL, headers=headers, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        enlaces_juegos = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            if href.endswith('-free-download/') and 'category' not in href:
                url_completa = urljoin(BASE_URL, href)
                enlaces_juegos.append(url_completa)

        enlaces_juegos = list(set(enlaces_juegos))
        print(f"Encontrados {len(enlaces_juegos)} juegos reales para procesar.")

        for i, url_juego in enumerate(enlaces_juegos[:15]):
            print(f"Procesando {i+1}/{len(enlaces_juegos[:15])}: {url_juego}")
            
            titulo_real, nuevos_enlaces = obtener_enlaces_de_juego(url_juego)
            
            if not titulo_real:
                titulo_real = url_juego.strip('/').split('/')[-1].replace('-free-download', '').replace('-', ' ').title()
            
            if not nuevos_enlaces:
                print(f"  No se encontraron enlaces para {titulo_real}")
                continue

            print(f"  ✓ {titulo_real}: {len(nuevos_enlaces)} enlace(s) encontrado(s)")
            for e in nuevos_enlaces:
                print(f"      - {e}")

            if titulo_real in mapa_juegos:
                if set(mapa_juegos[titulo_real].get("uris", [])) != set(nuevos_enlaces):
                    print(f"  → Actualizando {titulo_real}")
                    mapa_juegos[titulo_real]["uris"] = nuevos_enlaces
                else:
                    print(f"  → {titulo_real} ya está actualizado.")
            else:
                print(f"  → Añadiendo nuevo juego: {titulo_real}")
                mapa_juegos[titulo_real] = {
                    "title": titulo_real,
                    "uris": nuevos_enlaces,
                    "uploadDate": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "fileSize": "Desconocido",
                    "repacker": "SteamRIP"
                }
            
            time.sleep(1)

    except Exception as e:
        print(f"Error en el scraping principal: {e}")

    output = {
        "name": "SteamRIP (Auto)",
        "downloads": list(mapa_juegos.values())
    }
    
    with open('steamrip_auto.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\nProceso completado. Total: {len(mapa_juegos)} juegos.")

if __name__ == "__main__":
    scrape_steamrip()
