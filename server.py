from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
import logging
from typing import Optional, List, Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("casadellibro")

app = FastAPI(title="Casa del Libro API")

_pw = None
_browser = None
_context = None

sem = asyncio.Semaphore(1)


class ScrapeResult(BaseModel):
    source: str
    isbn: str
    status: str
    title: Optional[str] = None
    price_current_eur: Optional[str] = None
    price_previous_eur: Optional[str] = None
    url: Optional[str] = None
    status_code: Optional[int] = None
    error: Optional[str] = None
    debug_html_preview: Optional[str] = None
    debug_screenshot_path: Optional[str] = None


class BatchRequest(BaseModel):
    isbns: List[str] = Field(..., min_length=1, max_length=50)
    pause_ms: int = Field(default=1500, ge=0, le=10000)


class BatchResponse(BaseModel):
    source: str
    count: int
    success_count: int
    error_count: int
    results: List[ScrapeResult]


def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")


async def create_context():
    global _browser
    return await _browser.new_context(
        locale="es-ES",
        timezone_id="Europe/Madrid",
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )


async def get_context():
    global _context
    if _context is None:
        _context = await create_context()
    return _context


@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    _context = await create_context()
    logger.info("Browser initialized")


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
    return {"version": "2026-03-17-safe-debug-v1"}


async def accept_cookies(page) -> None:
    selectors = [
        "#onetrust-accept-btn-handler",
        'button[data-testid="cookie-accept"]',
        ".cookie-accept",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=2000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            pass


async def detect_no_results(page) -> bool:
    patterns = [
        "No se han encontrado resultados",
        "No se encontraron resultados",
        "No hay resultados",
    ]
    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
        return any(p.lower() in body_text.lower() for p in patterns)
    except Exception:
        return False


async def save_debug_artifacts(page, isbn: str) -> Dict[str, Optional[str]]:
    screenshot_path = f"/tmp/casadellibro_{isbn}.png"
    html_preview = None

    try:
        await page.screenshot(path=screenshot_path, full_page=True)
    except Exception:
        screenshot_path = None

    try:
        html_preview = (await page.content())[:3000]
    except Exception:
        html_preview = None

    return {
        "debug_screenshot_path": screenshot_path,
        "debug_html_preview": html_preview,
    }


async def scrape_casadellibro_one(isbn: str) -> ScrapeResult:
    isbn = clean_isbn(isbn)
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    if not (10 <= len(isbn) <= 13) or not isbn.isdigit():
        return ScrapeResult(
            source="casadellibro",
            isbn=isbn,
            status="invalid_input",
            url=url,
            error="ISBN inválido (debe tener 10-13 dígitos)",
        )

    async with sem:
        ctx = await get_context()
        page = await ctx.new_page()

        try:
            logger.info(f"Scraping ISBN {isbn}")
            response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)

            status_code = response.status if response else None
            logger.info(f"HTTP status for {isbn}: {status_code}")

            if status_code in (403, 429, 503):
                debug = await save_debug_artifacts(page, isbn)
                return ScrapeResult(
                    source="casadellibro",
                    isbn=isbn,
                    status="blocked",
                    url=url,
                    status_code=status_code,
                    error=f"Source blocked request with HTTP {status_code}",
                    debug_html_preview=debug["debug_html_preview"],
                    debug_screenshot_path=debug["debug_screenshot_path"],
                )

            await page.wait_for_timeout(1500)
            await accept_cookies(page)

            if await detect_no_results(page):
                return ScrapeResult(
                    source="casadellibro",
                    isbn=isbn,
                    status="not_found",
                    url=page.url,
                    status_code=status_code,
                    error="No se encontraron resultados para este ISBN",
                )

            title = None
            price_current = None
            price_previous = None

            title_selectors = [
                '[data-test="result-title"]',
                "h1",
                "h2",
                ".title",
                ".product-title",
            ]
            for sel in title_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        txt = (await loc.inner_text(timeout=2000)).strip()
                        if txt:
                            title = txt
                            break
                except Exception:
                    pass

            current_price_selectors = [
                '[data-test="result-current-price"] span.x-currency',
                '[data-test="result-current-price"]',
                ".x-currency",
                ".price",
            ]
            for sel in current_price_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        txt = (await loc.inner_text(timeout=2000)).strip()
                        value = normalize_price(txt)
                        if value:
                            price_current = value
                            break
                except Exception:
                    pass

            previous_price_selectors = [
                '[data-test="result-previous-price"] span.x-currency',
                '[data-test="result-previous-price"]',
                ".old-price",
            ]
            for sel in previous_price_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        txt = (await loc.inner_text(timeout=2000)).strip()
                        value = normalize_price(txt)
                        if value:
                            price_previous = value
                            break
                except Exception:
                    pass

            if not price_current:
                debug = await save_debug_artifacts(page, isbn)
                return ScrapeResult(
                    source="casadellibro",
                    isbn=isbn,
                    status="parse_error",
                    title=title,
                    url=page.url,
                    status_code=status_code,
                    error="Precio no encontrado en la página",
                    debug_html_preview=debug["debug_html_preview"],
                    debug_screenshot_path=debug["debug_screenshot_path"],
                )

            return ScrapeResult(
                source="casadellibro",
                isbn=isbn,
                status="ok",
                title=title,
                price_current_eur=price_current,
                price_previous_eur=price_previous,
                url=page.url,
                status_code=status_code,
            )

        except PlaywrightTimeoutError:
            return ScrapeResult(
                source="casadellibro",
                isbn=isbn,
                status="timeout",
                url=url,
                error="Timeout al cargar la página",
            )

        except Exception as e:
            logger.exception(f"Unexpected error for {isbn}")
            return ScrapeResult(
                source="casadellibro",
                isbn=isbn,
                status="error",
                url=url,
                error=str(e),
            )

        finally:
            await page.close()


@app.get("/casadellibro", response_model=ScrapeResult)
async def casadellibro(isbn: str = Query(..., min_length=10, max_length=13)):
    return await scrape_casadellibro_one(isbn)


@app.post("/casadellibro/batch", response_model=BatchResponse)
async def casadellibro_batch(req: BatchRequest):
    isbns = list(dict.fromkeys(clean_isbn(x) for x in req.isbns if clean_isbn(x)))
    results: List[ScrapeResult] = []

    for i, isbn in enumerate(isbns, start=1):
        logger.info(f"Batch [{i}/{len(isbns)}] ISBN {isbn}")
        result = await scrape_casadellibro_one(isbn)
        results.append(result)

        if i < len(isbns) and req.pause_ms > 0:
            await asyncio.sleep(req.pause_ms / 1000)

    success_count = sum(1 for r in results if r.status == "ok")
    error_count = len(results) - success_count

    return BatchResponse(
        source="casadellibro",
        count=len(results),
        success_count=success_count,
        error_count=error_count,
        results=results,
    )