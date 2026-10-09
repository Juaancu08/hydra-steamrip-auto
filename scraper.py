import requests
from bs4 import BeautifulSoup
import json
import time
import re
from urllib.parse import urljoin

BASE_URL = "https://steamrip.com/"
GAMES_LIST_URL = "https://steamrip.com/games-list-page/"
SOURCE_URL = "https://hydralinks.cloud/sources/steamrip.json"

def limpiar_titulo(titulo):
    """Limpia el título quitando 'Free Download' y todo lo que venga después."""
    titulo = re.sub(r'\s*Free Download.*$', '', titulo, flags=re.IGNORECASE)
    return titulo.strip()

def extraer_metadatos(soup):
    """Extrae metadatos de la página del juego (tamaño, versión, género, etc.)."""
    metadatos = {}
    
    for li in soup.find_all('li'):
        strong = li.find('strong')
        if not strong:
            continue
        
        etiqueta = strong.get_text(strip=True).rstrip(':').strip()
        # El valor es el texto del li sin el strong
        valor = li.get_text(strip=True).replace(strong.get_text(strip=True), '').strip()
        valor = valor.lstrip(':').strip()
        
        if not valor:
            continue
        
        etiqueta_lower = etiqueta.lower()
        if 'game size' in etiqueta_lower or 'size' in etiqueta_lower:
            metadatos['fileSize'] = valor
        elif 'version' in etiqueta_lower:
            metadatos['version'] = valor
        elif 'genre' in etiqueta_lower:
            metadatos['genre'] = valor
        elif 'developer' in etiqueta_lower:
            metadatos['developer'] = valor
    
    return metadatos

def obtener_datos_de_juego(url_juego, session):
    """Extrae enlaces de descarga Y metadatos de un juego."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        response = session.get(url_juego, headers=headers, timeout=20)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        enlaces = []
        titulo_real = None
        
        # Título desde el <h1>
        h1 = soup.find('h1')
        if h1:
            titulo_real = limpiar_titulo(h1.get_text(strip=True))
        
        # Enlaces de descarga
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
        
        # Metadatos
        metadatos = extraer_metadatos(soup)
        
        return titulo_real, list(set(enlaces)), metadatos
        
    except Exception as e:
        print(f"  Error: {e}")
        return None, [], {}

def obtener_lista_juegos(session):
    """Obtiene TODOS los juegos desde la página de lista completa."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    print(f"Descargando lista completa desde {GAMES_LIST_URL}")
    response = session.get(GAMES_LIST_URL, headers=headers, timeout=60)
    response.raise_for_status()
    
    soup = BeautifulSoup(response.text, 'html.parser')
    juegos = {}
    
    for li in soup.find_all('li', class_='az-list-item'):
        a_tag = li.find('a', href=True)
        if not a_tag:
            continue
        
        href = a_tag['href']
        texto = a_tag.get_text(strip=True)
        
        if href.endswith('-free-download/') and texto and 'Free Download' in texto:
            url_completa = urljoin(BASE_URL, href)
            titulo_limpio = limpiar_titulo(texto)
            if titulo_limpio and titulo_limpio not in juegos:
                juegos[titulo_limpio] = url_completa
    
    print(f"Encontrados {len(juegos)} juegos en la lista.")
    return juegos

def scrape_steamrip():
    session = requests.Session()
    juegos_data = []
    
    # 1. Cargar datos base
    try:
        print("Cargando datos base desde HydraLinks...")
        resp = session.get(SOURCE_URL, timeout=30)
        if resp.status_code == 200:
            data_original = resp.json()
            if isinstance(data_original, dict) and "downloads" in data_original:
                juegos_data = data_original["downloads"]
                print(f"Cargados {len(juegos_data)} juegos existentes.")
    except Exception as e:
        print(f"No se pudieron cargar los datos base: {e}")

    mapa_juegos = {limpiar_titulo(j["title"]): j for j in juegos_data}

    # 2. Lista completa
    try:
        lista_juegos = obtener_lista_juegos(session)
    except Exception as e:
        print(f"Error obteniendo lista: {e}")
        return
    
    if not lista_juegos:
        print("No se encontraron juegos. Abortando.")
        return

    # 3. Procesar cada juego
    total = len(lista_juegos)
    nuevos = actualizados = sin_cambios = fallidos = 0
    
    for i, (titulo, url_juego) in enumerate(lista_juegos.items()):
        print(f"Procesando {i+1}/{total}: {titulo}")
        
        titulo_real, nuevos_enlaces, metadatos = obtener_datos_de_juego(url_juego, session)
        
        if not titulo_real:
            titulo_real = titulo
        
        if not nuevos_enlaces:
            print(f"  ⚠ Sin enlaces")
            fallidos += 1
            continue

        entrada = {
            "title": titulo_real,
            "uris": nuevos_enlaces,
            "uploadDate": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "fileSize": metadatos.get('fileSize', 'Desconocido'),
            "repacker": "SteamRIP"
        }
        
        if 'version' in metadatos:
            entrada['version'] = metadatos['version']
        if 'genre' in metadatos:
            entrada['genre'] = metadatos['genre']
        if 'developer' in metadatos:
            entrada['developer'] = metadatos['developer']

        if titulo_real in mapa_juegos:
            uris_actuales = set(mapa_juegos[titulo_real].get("uris", []))
            if uris_actuales != set(nuevos_enlaces):
                print(f"  → Actualizado ({len(nuevos_enlaces)} enlaces, {metadatos.get('fileSize', '?')})")
                mapa_juegos[titulo_real].update(entrada)
                actualizados += 1
            else:
                print(f"  ✓ Sin cambios")
                sin_cambios += 1
        else:
            print(f"  ★ Nuevo ({len(nuevos_enlaces)} enlaces, {metadatos.get('fileSize', '?')})")
            mapa_juegos[titulo_real] = entrada
            nuevos += 1
        
        time.sleep(0.3)

    # 4. Guardar
    output = {
        "name": "SteamRIP (Auto)",
        "downloads": list(mapa_juegos.values())
    }
    
    with open('steamrip_auto.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    
    print(f"\n{'='*50}")
    print(f"✓ Proceso completado")
    print(f"  Total en la fuente: {len(mapa_juegos)} juegos")
    print(f"  Nuevos: {nuevos} | Actualizados: {actualizados}")
    print(f"  Sin cambios: {sin_cambios} | Fallidos: {fallidos}")
    print(f"{'='*50}")

if __name__ == "__main__":
    scrape_steamrip()
