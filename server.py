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
    for _ in range(3):
        try:
            btn = page.get_by_role("button", name=re.compile(r"(aceptar|acepto|agree)", re.I)).first
            await btn.click(timeout=2500)
            await page.wait_for_timeout(300)
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
    return {"version": "2026-01-09-data-test-only-v1"}


# --- scraping reutilizable (misma lógica que tu script local) ---
async def scrape_casadellibro_one(isbn: str) -> Dict[str, Any]:
    global _context
    isbn = clean_isbn(isbn)
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    # validación básica
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
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await accept_cookies(page)

            # si no hay resultados, corta antes de cualquier cosa
            if await has_no_results(page):
                return {
                    "isbn": isbn,
                    "price_current_eur": None,
                    "price_previous_eur": None,
                    "url": page.url,
                    "error": "No results for this ISBN",
                }

            # IMPORTANTÍSIMO:
            # esperamos SOLO el precio del resultado (no span.x-currency genérico)
            await page.wait_for_selector(
                '[data-test="result-current-price"] span.x-currency',
                timeout=30000,
            )

            current_text = None
            previous_text = None

            # current price (siempre)
            cur_loc = page.locator('[data-test="result-current-price"] span.x-currency').first
            try:
                current_text = (await cur_loc.inner_text(timeout=5000)).strip()
            except Exception:
                current_text = None

            # previous price (solo si hay descuento)
            prev_loc = page.locator('[data-test="result-previous-price"] span.x-currency').first
            try:
                # si no existe, no pasa nada
                if await prev_loc.count() > 0:
                    previous_text = (await prev_loc.inner_text(timeout=2000)).strip()
            except Exception:
                previous_text = None

            current_price = normalize_price(current_text)
            previous_price = normalize_price(previous_text)

            # si no hay precio del resultado, no busques nada más (nada de recomendaciones)
            if not current_price:
                return {
                    "isbn": isbn,
                    "price_current_eur": None,
                    "price_previous_eur": None,
                    "url": page.url,
                    "error": "Price not found",
                }

            return {
                "isbn": isbn,
                "price_current_eur": current_price,
                "price_previous_eur": previous_price,  # None si no hay descuento
                "url": page.url,
                "error": None,
            }

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
