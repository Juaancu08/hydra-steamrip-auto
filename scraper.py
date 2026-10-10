"""
scraper.py — Fuente automática de SteamRIP para Hydra Launcher
===============================================================
Genera y mantiene actualizado un archivo JSON con todos los juegos de SteamRIP.com.
Diseñado para ejecutarse en GitHub Actions una vez al día.

Correcciones clave:
- Detecta todos los juegos con patrón '-free-download' (incluyendo sufijos como '-e1/', '-j1/', etc.)
- Corrige uploadDate para no capturar el nombre del cracker (ej: Mr_GOLDBERG)
- Descompresión nativa mediante 'gzip, deflate' sin dependencias externas
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import re
import os
import hashlib
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ═══════════════════════════════════════════════════════════════════

BASE_URL       = "https://steamrip.com/"
GAMES_LIST_URL = "https://steamrip.com/games-list-page/"
JSON_FILE      = "steamrip_auto.json"

MAX_WORKERS = 4      # Hilos paralelos conservadores para evitar bloqueos
REINTENTOS  = 3      # Intentos por petición fallida
PAUSA_BASE  = 1.5    # Segundos de espera inicial entre reintentos
TIMEOUT     = 25     # Segundos máximos de espera por petición

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection":      "keep-alive",
}


# ═══════════════════════════════════════════════════════════════════
# UTILIDADES GENERALES
# ═══════════════════════════════════════════════════════════════════

def limpiar_titulo(titulo: str) -> str:
    """
    Limpieza MÍNIMA del título para presentación visual.
    Solo elimina 'Free Download...' y 'Build XXXXX' para no alterar
    nombres reales, versiones o subtítulos.
    """
    titulo = re.sub(r'\s*Free\s+Download.*$', '', titulo, flags=re.IGNORECASE)
    titulo = re.sub(r'\s*Build\s+\d+\s*$',   '', titulo, flags=re.IGNORECASE)
    return titulo.strip()


def slug_desde_url(url: str) -> str:
    """Extrae el slug único de la URL para usarlo como clave primaria."""
    try:
        ruta = urlparse(url).path
        slug = ruta.strip("/").split("/")[-1]
        return slug if slug else "sin-slug"
    except Exception:
        return "sin-slug"


def huella_uris(uris: list) -> str:
    """Genera un hash MD5 ordenado para comparar enlaces."""
    hashes = sorted(hashlib.md5(u.strip().lower().encode()).hexdigest() for u in uris)
    return hashlib.md5("|".join(hashes).encode()).hexdigest()


def peticion(url: str, session: requests.Session) -> requests.Response | None:
    """Petición GET con reintentos y retroceso exponencial."""
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            codigo = e.response.status_code if e.response is not None else 0
            if 400 <= codigo < 500:
                return None
            pausa = PAUSA_BASE * (2 ** (intento - 1))
            time.sleep(pausa)
        except requests.exceptions.RequestException:
            pausa = PAUSA_BASE * (2 ** (intento - 1))
            time.sleep(pausa)
    return None


# ═══════════════════════════════════════════════════════════════════
# OBTENCIÓN DE LA LISTA DE JUEGOS
# ═══════════════════════════════════════════════════════════════════

def obtener_catalogo(session: requests.Session) -> dict:
    """Descarga el catálogo completo (~4.780 juegos)."""
    print(f"⬇  Descargando catálogo completo desde {GAMES_LIST_URL}...")
    resp = peticion(GAMES_LIST_URL, session)
    if resp is None:
        raise ConnectionError(f"No se pudo acceder a {GAMES_LIST_URL}.")

    soup   = BeautifulSoup(resp.text, "html.parser")
    juegos = {}

    for li in soup.find_all("li", class_="az-list-item"):
        a = li.find("a", href=True)
        if not a:
            continue

        href  = a["href"]
        texto = a.get_text(strip=True)

        # Captura todos los juegos que contengan '-free-download' (incluye sufijos como '-e1/', etc.)
        if "-free-download" not in href:
            continue
        if not texto or "Free Download" not in texto:
            continue

        url_completa = urljoin(BASE_URL, href)
        slug         = slug_desde_url(url_completa)

        if slug and slug not in juegos:
            juegos[slug] = url_completa

    print(f"✔  Catálogo cargado: {len(juegos)} juegos encontrados.\n")
    return juegos


# ═══════════════════════════════════════════════════════════════════
# EXTRACCIÓN DE DATOS POR JUEGO
# ═══════════════════════════════════════════════════════════════════

def extraer_metadatos(soup: BeautifulSoup) -> dict:
    """Extrae metadatos asegurando fechas válidas."""
    meta = {}

    for li in soup.find_all("li"):
        strong = li.find("strong")
        if not strong:
            continue

        etiqueta = strong.get_text(strip=True).rstrip(":").lower()
        valor    = li.get_text(strip=True).replace(strong.get_text(strip=True), "").lstrip(":").strip()
        if not valor:
            continue

        if "game size" in etiqueta or ("size" in etiqueta and "game" in etiqueta):
            meta["fileSize"] = valor
        elif etiqueta.startswith("version"):
            meta["version"] = valor
        elif "genre" in etiqueta:
            meta["genre"] = valor
        elif "developer" in etiqueta:
            meta["developer"] = valor
        # Solo capturamos fechas si contiene explícitamente 'date' (evita capturar el cracker en 'Release:')
        elif "date" in etiqueta:
            meta["releaseDate"] = valor

    # Preferir siempre la fecha ISO de la etiqueta <time> si existe
    time_tag = soup.find("time", {"datetime": True})
    if time_tag:
        meta["releaseDate"] = time_tag["datetime"]

    return meta


def procesar_juego(args: tuple) -> tuple:
    """Descarga y extrae datos y enlaces de descarga de un juego."""
    slug, url, session = args
    try:
        resp = peticion(url, session)
        if resp is None:
            return (slug, None, [], {}, f"Sin respuesta de {url}")

        soup    = BeautifulSoup(resp.text, "html.parser")
        titulo  = None
        enlaces = []

        h1 = soup.find("h1")
        if h1:
            titulo = limpiar_titulo(h1.get_text(strip=True))

        dominio_propio = urlparse(BASE_URL).netloc

        for a in soup.find_all("a", href=True):
            clases = a.get("class", [])
            if "shortc-button" not in clases:
                continue

            href = a["href"]

            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin(BASE_URL, href)

            if dominio_propio in urlparse(href).netloc:
                continue

            if href not in enlaces:
                enlaces.append(href)

        meta = extraer_metadatos(soup)
        return (slug, titulo, enlaces, meta, None)

    except Exception as e:
        return (slug, None, [], {}, str(e))


# ═══════════════════════════════════════════════════════════════════
# PROCESADO EN PARALELO Y FUSIÓN
# ═══════════════════════════════════════════════════════════════════

def procesar_catalogo(catalogo: dict, mapa: dict, session: requests.Session) -> tuple:
    """Descarga en paralelo y fusiona con el mapa existente."""
    total       = len(catalogo)
    nuevos      = actualizados = sin_cambios = fallidos = 0
    completados = 0

    tareas = [(slug, url, session) for slug, url in catalogo.items()]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(procesar_juego, t): t for t in tareas}

        for future in as_completed(futures):
            completados += 1
            slug, titulo, enlaces, meta, error = future.result()
            prefijo = f"[{completados:>4}/{total}]"

            if error:
                print(f"{prefijo} ❌ ERROR      {slug[:55]} → {error}")
                fallidos += 1
                continue

            if not enlaces:
                nombre = titulo or slug
                print(f"{prefijo} ⚠  SIN ENLACE {nombre[:55]}")
                fallidos += 1
                continue

            if not titulo:
                titulo = slug

            # Fecha garantizada en formato ISO
            fecha = meta.get("releaseDate") or \
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            entrada = {
                "title":      titulo,
                "slug":       slug,
                "uris":       enlaces,
                "uploadDate": fecha,
                "fileSize":   meta.get("fileSize", "Desconocido"),
                "repacker":   "SteamRIP",
            }
            for campo in ("version", "genre", "developer"):
                if campo in meta:
                    entrada[campo] = meta[campo]

            if slug in mapa:
                if huella_uris(mapa[slug].get("uris", [])) != huella_uris(enlaces):
                    print(f"{prefijo} 🔄 ACTUALIZADO {titulo[:53]}")
                    mapa[slug].update(entrada)
                    actualizados += 1
                else:
                    sin_cambios += 1
            else:
                print(f"{prefijo} ✅ NUEVO       {titulo[:53]}")
                mapa[slug] = entrada
                nuevos += 1

    return nuevos, actualizados, sin_cambios, fallidos


# ═══════════════════════════════════════════════════════════════════
# PUNTO DE ENTRADA
# ═══════════════════════════════════════════════════════════════════

def main():
    tiempo_inicio = time.time()
    session       = requests.Session()

    print("╔══════════════════════════════════════════════════════╗")
    print("║        SteamRIP → Hydra Source Updater              ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}                                    ║")
    print("╚══════════════════════════════════════════════════════╝\n")

    mapa = {}

    if os.path.exists(JSON_FILE):
        print(f"📂 Cargando datos previos desde {JSON_FILE}...")
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
            if isinstance(datos, dict) and "downloads" in datos:
                for entrada in datos["downloads"]:
                    slug = entrada.get("slug")
                    if slug:
                        mapa[slug] = entrada
            print(f"   → {len(mapa)} entradas cargadas del JSON local.\n")
        except Exception:
            mapa = {}
    else:
        print("🆕 Primera ejecución: empezando con catálogo limpio.\n")

    try:
        catalogo = obtener_catalogo(session)
    except ConnectionError as e:
        print(f"\n🚨 Error crítico: {e}")
        return

    print(f"⚙  Procesando {len(catalogo)} juegos con {MAX_WORKERS} hilos paralelos...\n")
    nuevos, actualizados, sin_cambios, fallidos = procesar_catalogo(catalogo, mapa, session)

    salida = {
        "name":      "SteamRIP (Auto)",
        "downloads": list(mapa.values()),
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    duracion      = time.time() - tiempo_inicio
    minutos, segs = divmod(int(duracion), 60)

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║                  RESUMEN FINAL                      ║")
    print("╠══════════════════════════════════════════════════════╣")
    print(f"║  Total en la fuente  : {len(mapa):>5} juegos               ║")
    print(f"║  ✅ Nuevos           : {nuevos:>5}                        ║")
    print(f"║  🔄 Actualizados     : {actualizados:>5}                        ║")
    print(f"║  ➖ Sin cambios      : {sin_cambios:>5}                        ║")
    print(f"║  ❌ Fallidos         : {fallidos:>5}                        ║")
    print(f"║  ⏱  Duración total   : {minutos}m {segs:02d}s                      ║")
    print(f"║  💾 Guardado en      : {JSON_FILE:<29} ║")
    print("╚══════════════════════════════════════════════════════╝\n")


if __name__ == "__main__":
    main()
