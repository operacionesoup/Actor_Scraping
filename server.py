# server.py
from fastapi import FastAPI, Query
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
from typing import Optional, List, Dict, Any

app = FastAPI(title="Casa del Libro API")

_pw = None
_browser = None
_context = None

# limita concurrencia para que no revientes el navegador
sem = asyncio.Semaphore(2)

def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "")

def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")

async def accept_cookies(page) -> None:
    # no bloquea: intenta, si no, sigue
    for _ in range(2):
        try:
            btn = page.get_by_role("button", name=re.compile(r"(aceptar|acepto|agree)", re.I)).first
            await btn.click(timeout=1500)
            await page.wait_for_timeout(250)
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

async def wait_for_result_card(page, isbn: str, timeout=25000):
    """
    Espera el article del resultado correcto anclando por href con ISBN.
    """
    main = page.locator("main")

    # selector robusto: article que contenga un link con el isbn
    card = main.locator(f'article:has(a[href*="{isbn}"])').first
    await card.wait_for(state="visible", timeout=timeout)
    return card

async def extract_prices_from_card(card) -> Dict[str, Optional[str]]:
    """
    Usa data-test (estable) y fallback a spans dentro del card.
    """
    current_text = None
    previous_text = None

    # current (precio actual)
    current_candidates = [
        card.locator('[data-test="result-current-price"] span.x-currency').first,
        card.locator(".x-result-current-price-on-sale span.x-currency").first,
        card.locator(".x-result-current-price span.x-currency").first,
    ]
    for loc in current_candidates:
        try:
            if await loc.count() > 0 and await loc.is_visible():
                current_text = (await loc.inner_text()).strip()
                if current_text:
                    break
        except Exception:
            pass

    # previous (tachado)
    prev_candidates = [
        card.locator("del span.x-currency").first,
        card.locator(".x-result-previous-price span.x-currency").first,
        card.locator('[data-test="result-previous-price"] span.x-currency').first,
    ]
    for loc in prev_candidates:
        try:
            if await loc.count() > 0 and await loc.is_visible():
                previous_text = (await loc.inner_text()).strip()
                if previous_text:
                    break
        except Exception:
            pass

    # fallback final: si no encontró current por data-test, coge el primer x-currency del card
    if not current_text:
        try:
            fallback = card.locator("span.x-currency:visible").first
            if await fallback.count() > 0:
                current_text = (await fallback.inner_text()).strip()
        except Exception:
            pass

    return {
        "current": normalize_price(current_text),
        "previous": normalize_price(previous_text),
    }

@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()

    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )

    _context = await _browser.new_context(
        locale="es-ES",
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
    )

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
    return {"version": "2026-01-07-timeout-fix-card-scoped-v1"}

# --- scraping reutilizable ---
async def scrape_casadellibro_one(isbn: str) -> Dict[str, Any]:
    global _context
    isbn = clean_isbn(isbn)
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    if not (10 <= len(isbn) <= 13) or not isbn.isdigit():
        return {
            "isbn": isbn,
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": url,
            "error": "Invalid ISBN (must be 10-13 digits)",
        }

    async with sem:
        page = await _context.new_page()
        try:
            # retries cortos: Railway a veces va lento
            last_err = None
            for attempt in range(1, 4):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
                    await accept_cookies(page)

                    # no dependemos de networkidle (en SPA a veces nunca llega)
                    # esperamos a que aparezca el resultado
                    if await has_no_results(page):
                        return {
                            "isbn": isbn,
                            "price_current_eur": None,
                            "price_previous_eur": None,
                            "url": page.url,
                            "error": "No results for this ISBN",
                        }

                    card = await wait_for_result_card(page, isbn, timeout=25000)
                    prices = await extract_prices_from_card(card)

                    if not prices["current"]:
                        return {
                            "isbn": isbn,
                            "price_current_eur": None,
                            "price_previous_eur": None,
                            "url": page.url,
                            "error": "Price not found in result card",
                        }

                    return {
                        "isbn": isbn,
                        "price_current_eur": prices["current"],
                        "price_previous_eur": prices["previous"],
                        "url": page.url,
                        "error": None,
                    }

                except Exception as e:
                    last_err = e
                    # pequeña pausa y reintento
                    await page.wait_for_timeout(500 * attempt)

            # si falló todo
            raise last_err

        except PlaywrightTimeoutError:
            return {
                "isbn": isbn,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": url,
                "error": f"Timeout ({isbn})",
            }
        except Exception as e:
            return {
                "isbn": isbn,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": url,
                "error": str(e),
            }
        finally:
            await page.close()

# --- endpoint unitario ---
@app.get("/casadellibro")
async def casadellibro(isbn: str = Query(..., min_length=10, max_length=13)):
    return await scrape_casadellibro_one(isbn)

# --- endpoint batch ---
class BatchRequest(BaseModel):
    isbns: List[str] = Field(..., min_items=1, description="Lista de ISBNs (10-13 dígitos)")

class BatchResponse(BaseModel):
    source: str
    count: int
    results: List[Dict[str, Any]]

@app.post("/casadellibro/batch", response_model=BatchResponse)
async def casadellibro_batch(req: BatchRequest):
    isbns = [clean_isbn(x) for x in req.isbns if clean_isbn(x)]
    if not isbns:
        return {"source": "casadellibro", "count": 0, "results": []}

    tasks = [scrape_casadellibro_one(isbn) for isbn in isbns]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    fixed_results: List[Dict[str, Any]] = []
    for isbn, r in zip(isbns, results):
        if isinstance(r, Exception):
            fixed_results.append({
                "isbn": isbn,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": f"https://www.casadellibro.com/libros?query={isbn}",
                "error": str(r),
            })
        else:
            fixed_results.append(r)

    return {"source": "casadellibro", "count": len(fixed_results), "results": fixed_results}
