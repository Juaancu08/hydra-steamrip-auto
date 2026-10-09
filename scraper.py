"""
scraper.py — Fuente automática de SteamRIP para Hydra Launcher
===============================================================
Genera y mantiene actualizado un archivo JSON con todos los juegos de SteamRIP.com.
Diseñado para ejecutarse en GitHub Actions una vez al día.

Decisiones de diseño:
- SIEMPRE modo completo: recorre los ~2600 juegos del catálogo en cada ejecución.
  Garantiza que nunca se pierda un juego nuevo ni una actualización de enlaces.
- Primera ejecución limpia: si no existe el JSON local, empieza vacío (sin cargar
  datos de HydraLinks). Así se evitan duplicados por diferencia de slug.
- Clave única = slug de URL: "doom-2016-free-download" nunca colisiona con
  "doom-free-download" aunque los títulos limpios fueran iguales.
- Limpieza mínima de título: solo quita "Free Download ..." y "Build XXXXXXXX".
  No toca versiones, años, ni subtítulos para preservar identidad del juego.
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

MAX_WORKERS = 4      # Hilos paralelos. 4 es conservador para evitar bloqueos de Cloudflare.
REINTENTOS  = 3      # Intentos por petición fallida antes de abandonar.
PAUSA_BASE  = 1.5    # Segundos de espera inicial entre reintentos (se dobla en cada intento).
TIMEOUT     = 25     # Segundos máximos de espera por petición.

# Cabeceras que imitan a un navegador Chrome real con preferencia de idioma español.
# Imprescindible para pasar los filtros básicos de Cloudflare con requests.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
}


# ═══════════════════════════════════════════════════════════════════
# UTILIDADES GENERALES
# ═══════════════════════════════════════════════════════════════════

def limpiar_titulo(titulo: str) -> str:
    """
    Limpieza MÍNIMA del título para presentación visual.

    Solo elimina los sufijos que SteamRIP añade mecánicamente en los H1:
      - "Free Download" y todo lo que sigue  → "Enshrouded Free Download (v0.9.1.2)" → "Enshrouded"
      - "Build XXXXXXXX" al final            → "Enshrouded Build 23008567"           → "Enshrouded"

    NO se eliminan: versiones (v1.x), años entre paréntesis, subtítulos ni guiones.
    Eso lo gestiona el slug de URL, que es la clave de identidad real del juego.
    """
    titulo = re.sub(r'\s*Free\s+Download.*$', '', titulo, flags=re.IGNORECASE)
    titulo = re.sub(r'\s*Build\s+\d+\s*$',   '', titulo, flags=re.IGNORECASE)
    return titulo.strip()


def slug_desde_url(url: str) -> str:
    """
    Extrae el slug único de una URL de SteamRIP para usarlo como clave primaria.

    Ejemplos:
      "https://steamrip.com/doom-2016-free-download/" → "doom-2016-free-download"
      "https://steamrip.com/doom-free-download/"      → "doom-free-download"

    Dos juegos con títulos similares nunca colisionan porque sus URLs son distintas.
    Si la URL no tiene path reconocible, devuelve "sin-slug" como fallback seguro.
    """
    try:
        ruta = urlparse(url).path              # "/doom-2016-free-download/"
        slug = ruta.strip("/").split("/")[-1]  # "doom-2016-free-download"
        return slug if slug else "sin-slug"
    except Exception:
        return "sin-slug"


def huella_uris(uris: list) -> str:
    """
    Genera un hash MD5 que representa un conjunto de URIs de forma estable,
    independientemente del orden en que vengan. Se usa para detectar si los
    enlaces de descarga de un juego cambiaron entre ejecuciones.
    """
    hashes = sorted(hashlib.md5(u.strip().lower().encode()).hexdigest() for u in uris)
    return hashlib.md5("|".join(hashes).encode()).hexdigest()


def peticion(url: str, session: requests.Session) -> requests.Response | None:
    """
    Realiza una petición GET con reintentos y espera exponencial entre fallos.

    - Errores 4xx (403, 404): fallo permanente, no se reintenta.
    - Errores 5xx o de red: fallo temporal, se reintenta hasta REINTENTOS veces.
    - Devuelve la Response si tuvo éxito, o None si todos los reintentos fallaron.
    """
    for intento in range(1, REINTENTOS + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            codigo = e.response.status_code if e.response is not None else 0
            if 400 <= codigo < 500:
                # Fallo permanente (recurso no existe o acceso bloqueado)
                return None
            pausa = PAUSA_BASE * (2 ** (intento - 1))
            print(f"    ↺ HTTP {codigo} en intento {intento}/{REINTENTOS}. Esperando {pausa:.1f}s...")
            time.sleep(pausa)
        except requests.exceptions.RequestException as e:
            pausa = PAUSA_BASE * (2 ** (intento - 1))
            print(f"    ↺ Error de red en intento {intento}/{REINTENTOS}: {e}. Esperando {pausa:.1f}s...")
            time.sleep(pausa)
    return None


# ═══════════════════════════════════════════════════════════════════
# OBTENCIÓN DE LA LISTA DE JUEGOS
# ═══════════════════════════════════════════════════════════════════

def obtener_catalogo(session: requests.Session) -> dict:
    """
    Descarga la página de índice completo de SteamRIP y extrae todos los juegos.

    Estructura HTML relevante:
      <li class="az-list-item">
        <a href="/nombre-del-juego-free-download/">Nombre Free Download (v1.2)</a>
      </li>

    Devuelve: {slug → url_completa}
      Ejemplo: {"doom-2016-free-download": "https://steamrip.com/doom-2016-free-download/"}
    """
    print(f"⬇  Descargando catálogo completo desde {GAMES_LIST_URL}...")
    resp = peticion(GAMES_LIST_URL, session)
    if resp is None:
        raise ConnectionError(f"No se pudo acceder a {GAMES_LIST_URL} después de {REINTENTOS} intentos.")

    soup    = BeautifulSoup(resp.text, "html.parser")
    juegos  = {}

    for li in soup.find_all("li", class_="az-list-item"):
        a = li.find("a", href=True)
        if not a:
            continue

        href  = a["href"]
        texto = a.get_text(strip=True)

        # Solo procesar entradas que sean páginas de juego individuales
        if not href.endswith("-free-download/"):
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
    """
    Extrae los metadatos de la página individual de un juego.

    Estructura HTML relevante:
      <ul>
        <li><strong>Genre:</strong> Action, RPG</li>
        <li><strong>Developer:</strong> SEGA</li>
        <li><strong>Game Size: </strong>27.7 GB</li>
        <li><strong>Version</strong>: v0.9.1.2</li>
      </ul>
      <time datetime="2026-05-10T...">...</time>  ← fecha de publicación del artículo
    """
    meta = {}

    for li in soup.find_all("li"):
        strong = li.find("strong")
        if not strong:
            continue

        etiqueta = strong.get_text(strip=True).rstrip(":").lower()
        # El valor es el texto del <li> sin el texto del <strong>
        valor = li.get_text(strip=True)
        valor = valor.replace(strong.get_text(strip=True), "").lstrip(":").strip()
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
        elif "release" in etiqueta or "date" in etiqueta:
            meta["releaseDate"] = valor

    # Intentar obtener la fecha real del artículo desde la etiqueta HTML5 <time>
    time_tag = soup.find("time", {"datetime": True})
    if time_tag and "releaseDate" not in meta:
        meta["releaseDate"] = time_tag["datetime"]

    return meta


def procesar_juego(args: tuple) -> tuple:
    """
    Descarga y parsea la página individual de un juego.

    Recibe: (slug, url, session)
    Devuelve: (slug, titulo, enlaces, metadatos, error_o_None)

    Extracción de enlaces:
      - Busca etiquetas <a class="shortc-button ...">
      - Excluye las que apunten de vuelta al propio dominio de SteamRIP
      - Normaliza URLs relativas (/path) y sin esquema (//host/path)
    """
    slug, url, session = args
    try:
        resp = peticion(url, session)
        if resp is None:
            return (slug, None, [], {}, f"Sin respuesta tras {REINTENTOS} intentos")

        soup    = BeautifulSoup(resp.text, "html.parser")
        titulo  = None
        enlaces = []

        # Título real desde el H1 de la página
        h1 = soup.find("h1")
        if h1:
            titulo = limpiar_titulo(h1.get_text(strip=True))

        # Dominio propio para excluir enlaces internos de SteamRIP
        dominio_propio = urlparse(BASE_URL).netloc

        for a in soup.find_all("a", href=True):
            clases = a.get("class", [])
            if "shortc-button" not in clases:
                continue

            href = a["href"]

            # Normalizar URLs sin esquema (//host/path) o relativas (/path)
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin(BASE_URL, href)

            # Excluir enlaces que vuelven al propio dominio de SteamRIP
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
    """
    Descarga en paralelo las páginas de todos los juegos del catálogo y fusiona
    los resultados en el mapa existente.

    Lógica de fusión por slug:
      - Si el slug ya existe en el mapa Y los URIs cambiaron → ACTUALIZADO
      - Si el slug ya existe en el mapa Y los URIs son iguales → SIN CAMBIO
      - Si el slug no existe en el mapa → NUEVO

    Devuelve: (nuevos, actualizados, sin_cambios, fallidos)
    """
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

            # ── Caso 1: Error de red o acceso denegado ──────────────────────
            if error:
                print(f"{prefijo} ❌ ERROR      {slug[:55]} → {error}")
                fallidos += 1
                continue

            # ── Caso 2: Página sin enlaces de descarga ──────────────────────
            if not enlaces:
                nombre = titulo or slug
                print(f"{prefijo} ⚠  SIN ENLACE {nombre[:55]}")
                fallidos += 1
                continue

            # Si no se extrajo título del H1, usar el slug como fallback
            if not titulo:
                titulo = slug

            # Fecha real del artículo o fecha actual como último recurso
            fecha = meta.get("releaseDate") or \
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

            # Construir la entrada en el formato que espera Hydra
            entrada = {
                "title":      titulo,
                "slug":       slug,       # clave permanente para ejecuciones futuras
                "uris":       enlaces,
                "uploadDate": fecha,
                "fileSize":   meta.get("fileSize", "Desconocido"),
                "repacker":   "SteamRIP",
            }
            # Campos opcionales: solo se añaden si están disponibles en la página
            for campo in ("version", "genre", "developer"):
                if campo in meta:
                    entrada[campo] = meta[campo]

            # ── Fusión en el mapa ────────────────────────────────────────────
            if slug in mapa:
                # El juego ya existía: comparar URIs con hash MD5
                if huella_uris(mapa[slug].get("uris", [])) != huella_uris(enlaces):
                    print(f"{prefijo} 🔄 ACTUALIZADO {titulo[:53]}")
                    mapa[slug].update(entrada)
                    actualizados += 1
                else:
                    # Sin cambios: actualizar solo metadatos no críticos si cambiaron
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

    # ── 1. Cargar JSON local si existe ──────────────────────────────────────────
    #
    # Si el archivo local ya existe de ejecuciones anteriores → cargarlo.
    # Si NO existe (primera ejecución) → empezar con JSON VACÍO.
    #
    # IMPORTANTE: No cargamos datos de HydraLinks como base porque sus entradas
    # no tienen campo "slug" y los slugs aproximados generados desde el título
    # colisionarían con los slugs reales de SteamRIP, causando duplicados.
    # Es más limpio y seguro construir el JSON completo desde cero en la primera
    # ejecución, que tarda ~30 min con MAX_WORKERS=4.
    #
    mapa = {}

    if os.path.exists(JSON_FILE):
        print(f"📂 Cargando datos previos desde {JSON_FILE}...")
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                datos = json.load(f)
            if isinstance(datos, dict) and "downloads" in datos:
                for entrada in datos["downloads"]:
                    slug = entrada.get("slug")
                    if not slug:
                        # Entrada sin slug (no debería ocurrir en ejecuciones normales,
                        # pero por robustez: generamos slug desde el título)
                        titulo_base = limpiar_titulo(entrada.get("title", "sin-titulo"))
                        slug = re.sub(r'[^a-z0-9]+', '-', titulo_base.lower()).strip('-') or "sin-slug"
                    mapa[slug] = entrada
            print(f"   → {len(mapa)} entradas cargadas del JSON local.\n")
        except (json.JSONDecodeError, IOError) as e:
            print(f"   ⚠ El archivo local estaba corrupto o ilegible ({e}). Empezando vacío.\n")
            mapa = {}
    else:
        print("🆕 Primera ejecución: empezando con JSON vacío.")
        print("   (No se carga HydraLinks para evitar duplicados por diferencia de slug)\n")

    # ── 2. Descargar el catálogo completo de SteamRIP ───────────────────────────
    #
    # SIEMPRE modo completo: recorre los ~2600 juegos del catálogo en cada ejecución.
    # Así se detectan tanto nuevos juegos como actualizaciones de juegos ya conocidos.
    # Con MAX_WORKERS=4 tarda ~25-35 min, bien dentro del límite de 6h de GitHub Actions.
    #
    try:
        catalogo = obtener_catalogo(session)
    except ConnectionError as e:
        print(f"\n🚨 Error crítico: {e}")
        print("   Abortando. El JSON no ha sido modificado.")
        return

    # ── 3. Procesar el catálogo en paralelo y fusionar con el mapa ──────────────
    print(f"⚙  Procesando {len(catalogo)} juegos con {MAX_WORKERS} hilos paralelos...\n")
    nuevos, actualizados, sin_cambios, fallidos = procesar_catalogo(catalogo, mapa, session)

    # ── 4. Guardar el JSON actualizado ──────────────────────────────────────────
    salida = {
        "name":      "SteamRIP (Auto)",
        "downloads": list(mapa.values()),
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(salida, f, indent=2, ensure_ascii=False)

    # ── 5. Resumen final ────────────────────────────────────────────────────────
    duracion       = time.time() - tiempo_inicio
    minutos, segs  = divmod(int(duracion), 60)

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
