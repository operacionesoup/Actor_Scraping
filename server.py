# server.py — Casa del Libro Scraper (stealth edition)
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
import random
import logging
from typing import Optional, List, Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("casadellibro")

app = FastAPI(title="Casa del Libro API")

_pw = None
_browser = None
_context = None
_request_count = 0  # reinicia contexto cada N peticiones

# concurrencia = 1 para ir secuencial y no levantar sospechas
sem = asyncio.Semaphore(1)

# ── User-Agent rotation ──────────────────────────────────────────────
USER_AGENTS = [
    # Chrome en Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome en Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Firefox en Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Safari en Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

VIEWPORTS = [
    {"width": 1280, "height": 720},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
]

# ── Stealth JS inyectado en cada contexto ────────────────────────────
STEALTH_JS = """
() => {
    // navigator.webdriver → false
    Object.defineProperty(navigator, 'webdriver', { get: () => false });

    // chrome runtime fake
    window.chrome = {
        runtime: { onConnect: { addListener: () => {} }, id: 'mocked' },
        loadTimes: () => ({}),
        csi: () => ({}),
    };

    // permisos normales
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(params);

    // plugins falsos
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
            { name: 'Native Client', filename: 'internal-nacl-plugin' },
        ],
    });

    // languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['es-ES', 'es', 'en-US', 'en'],
    });

    // hardware concurrency y device memory
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });

    // eliminar la traza de automation de CDP
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return 'Intel Inc.';
        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
        return getParameter.call(this, parameter);
    };
}
"""


def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")


def random_delay() -> float:
    """Delay aleatorio entre 2 y 5 segundos para simular humano."""
    return random.uniform(2.0, 5.0)


# ── Gestión del navegador ────────────────────────────────────────────
async def create_context():
    """Crea un contexto nuevo con UA y viewport aleatorios + stealth."""
    global _browser
    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)

    ctx = await _browser.new_context(
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport=vp,
        user_agent=ua,
        # simular pantalla real
        screen=vp,
        color_scheme="light",
        java_script_enabled=True,
        # extra HTTP headers para parecer navegador real
        extra_http_headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        },
    )

    # inyectar stealth antes de cada navegación
    await ctx.add_init_script(STEALTH_JS)
    return ctx


async def get_context():
    """Devuelve contexto actual o crea uno nuevo cada 10 peticiones."""
    global _context, _request_count
    _request_count += 1

    if _context is None or _request_count % 10 == 0:
        if _context:
            try:
                await _context.close()
            except Exception:
                pass
        _context = await create_context()
        logger.info(f"Nuevo contexto creado (petición #{_request_count})")

    return _context


@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()

    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--lang=es-ES",
        ],
    )

    _context = await create_context()
    logger.info("Navegador y contexto inicializados")


@app.on_event("shutdown")
async def shutdown():
    global _pw, _browser, _context
    try:
        if _context:
            await _context.close()
        if _browser:
            await _browser.close()
        if _pw:
            await _pw.stop()
    except Exception:
        pass


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/version")
async def version():
    return {"version": "2026-03-17-stealth-v2"}


# ── Aceptar cookies ─────────────────────────────────────────────────
async def accept_cookies(page) -> None:
    """Intenta cerrar el banner de cookies varias veces."""
    for _ in range(3):
        try:
            # botón por role
            btn = page.get_by_role(
                "button", name=re.compile(r"(aceptar|acepto|agree|accept)", re.I)
            ).first
            if await btn.count() > 0:
                await btn.click(timeout=3000)
                await page.wait_for_timeout(random.randint(300, 800))
                return
        except Exception:
            pass
        try:
            # fallback: por ID genérico de consent
            for sel in [
                "#onetrust-accept-btn-handler",
                'button[data-testid="cookie-accept"]',
                ".cookie-accept",
                'button:has-text("Aceptar")',
            ]:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=2000)
                    await page.wait_for_timeout(random.randint(300, 600))
                    return
        except Exception:
            pass


async def has_no_results(page) -> bool:
    try:
        loc = page.locator("text=/No se han encontrado resultados/i")
        if await loc.count() > 0 and await loc.first.is_visible():
            return True
    except Exception:
        pass
    return False


# ── Simular comportamiento humano ────────────────────────────────────
async def human_like_behavior(page) -> None:
    """Pequeños movimientos y scroll para parecer humano."""
    try:
        # scroll suave hacia abajo
        await page.evaluate("window.scrollBy({ top: 200, behavior: 'smooth' })")
        await page.wait_for_timeout(random.randint(300, 700))
        # mover el mouse a una posición aleatoria
        await page.mouse.move(
            random.randint(100, 800),
            random.randint(100, 500),
        )
        await page.wait_for_timeout(random.randint(200, 500))
    except Exception:
        pass


# ── Scraping principal ───────────────────────────────────────────────
async def scrape_casadellibro_one(isbn: str) -> Dict[str, Any]:
    isbn = clean_isbn(isbn)
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    if not (10 <= len(isbn) <= 13) or not isbn.isdigit():
        return {
            "isbn": isbn,
            "title": None,
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": url,
            "error": "ISBN inválido (debe tener 10-13 dígitos)",
        }

    async with sem:
        ctx = await get_context()
        page = await ctx.new_page()

        try:
            # delay aleatorio antes de la petición
            await asyncio.sleep(random_delay())

            logger.info(f"Scraping ISBN {isbn}...")

            # navegar
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            if resp and resp.status in (403, 429, 503):
                return {
                    "isbn": isbn,
                    "title": None,
                    "price_current_eur": None,
                    "price_previous_eur": None,
                    "url": url,
                    "error": f"Bloqueado por el servidor (HTTP {resp.status})",
                }

            # esperar un poco a que cargue JS
            await page.wait_for_timeout(random.randint(1500, 3000))

            # aceptar cookies
            await accept_cookies(page)

            # simular humano
            await human_like_behavior(page)

            # sin resultados
            if await has_no_results(page):
                return {
                    "isbn": isbn,
                    "title": None,
                    "price_current_eur": None,
                    "price_previous_eur": None,
                    "url": page.url,
                    "error": "No se encontraron resultados para este ISBN",
                }

            # esperar el selector de precio
            await page.wait_for_selector(
                '[data-test="result-current-price"] span.x-currency',
                timeout=30000,
            )

            # ── Extraer título ───────────────────────────────────
            title = None
            try:
                title_loc = page.locator('[data-test="result-title"]').first
                if await title_loc.count() > 0:
                    title = (await title_loc.inner_text(timeout=3000)).strip()
            except Exception:
                pass

            # ── Extraer precio actual ────────────────────────────
            current_text = None
            cur_loc = page.locator(
                '[data-test="result-current-price"] span.x-currency'
            ).first
            try:
                current_text = (await cur_loc.inner_text(timeout=5000)).strip()
            except Exception:
                current_text = None

            # ── Extraer precio anterior (si hay descuento) ───────
            previous_text = None
            prev_loc = page.locator(
                '[data-test="result-previous-price"] span.x-currency'
            ).first
            try:
                if await prev_loc.count() > 0:
                    previous_text = (await prev_loc.inner_text(timeout=2000)).strip()
            except Exception:
                previous_text = None

            current_price = normalize_price(current_text)
            previous_price = normalize_price(previous_text)

            if not current_price:
                return {
                    "isbn": isbn,
                    "title": title,
                    "price_current_eur": None,
                    "price_previous_eur": None,
                    "url": page.url,
                    "error": "Precio no encontrado en la página",
                }

            logger.info(f"ISBN {isbn} → {current_price} EUR")

            return {
                "isbn": isbn,
                "title": title,
                "price_current_eur": current_price,
                "price_previous_eur": previous_price,
                "url": page.url,
                "error": None,
            }

        except PlaywrightTimeoutError:
            return {
                "isbn": isbn,
                "title": None,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": url,
                "error": f"Timeout al cargar la página ({isbn})",
            }

        except Exception as e:
            logger.error(f"Error con ISBN {isbn}: {e}")
            return {
                "isbn": isbn,
                "title": None,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": url,
                "error": str(e),
            }
        finally:
            await page.close()


# ── Endpoint unitario ────────────────────────────────────────────────
@app.get("/casadellibro")
async def casadellibro(isbn: str = Query(..., min_length=10, max_length=13)):
    return await scrape_casadellibro_one(isbn)


# ── Endpoint batch (SECUENCIAL con delays) ───────────────────────────
class BatchRequest(BaseModel):
    isbns: List[str] = Field(
        ..., min_length=1, max_length=50,
        description="Lista de ISBNs (10-13 dígitos). Máximo 50 por lote."
    )
    delay_min: float = Field(
        default=3.0, ge=1.0, le=30.0,
        description="Delay mínimo entre peticiones (segundos)"
    )
    delay_max: float = Field(
        default=7.0, ge=2.0, le=60.0,
        description="Delay máximo entre peticiones (segundos)"
    )


class BatchResponse(BaseModel):
    source: str
    count: int
    success_count: int
    error_count: int
    results: List[Dict[str, Any]]


@app.post("/casadellibro/batch", response_model=BatchResponse)
async def casadellibro_batch(req: BatchRequest):
    isbns = list(dict.fromkeys(  # deduplica manteniendo orden
        [clean_isbn(x) for x in req.isbns if clean_isbn(x)]
    ))

    if not isbns:
        return {
            "source": "casadellibro",
            "count": 0,
            "success_count": 0,
            "error_count": 0,
            "results": [],
        }

    results: List[Dict[str, Any]] = []

    for i, isbn in enumerate(isbns):
        logger.info(f"Batch [{i+1}/{len(isbns)}] → ISBN {isbn}")

        try:
            result = await scrape_casadellibro_one(isbn)
        except Exception as e:
            result = {
                "isbn": isbn,
                "title": None,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": f"https://www.casadellibro.com/libros?query={isbn}",
                "error": str(e),
            }

        results.append(result)

        # delay entre peticiones (no después de la última)
        if i < len(isbns) - 1:
            delay = random.uniform(req.delay_min, req.delay_max)
            logger.info(f"  Esperando {delay:.1f}s antes del siguiente...")
            await asyncio.sleep(delay)

    success = sum(1 for r in results if r.get("error") is None)

    return {
        "source": "casadellibro",
        "count": len(results),
        "success_count": success,
        "error_count": len(results) - success,
        "results": results,
    }