# server.py
from fastapi import FastAPI, Query
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
import re
import asyncio
from typing import Optional, Dict

app = FastAPI(title="Casa del Libro API")

_pw = None
_browser = None
_context = None

# limita concurrencia para que no revientes el navegador
sem = asyncio.Semaphore(2)

def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")

async def accept_cookies(page) -> None:
    for _ in range(3):
        try:
            btn = page.get_by_role("button", name=re.compile(r"(aceptar|acepto|agree)", re.I)).first
            await btn.click(timeout=2500)
            await page.wait_for_timeout(500)
            return
        except Exception:
            pass

@app.on_event("startup")
async def startup():
    global _pw, _browser, _context
    _pw = await async_playwright().start()

    # en local: prueba primero con headless=False para confirmar que es el headless el culpable
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

@app.get("/casadellibro")
async def casadellibro(isbn: str = Query(..., min_length=10, max_length=13)):
    global _context
    isbn = isbn.strip().replace(" ", "")
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    async with sem:
        page = await _context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            await accept_cookies(page)

            # clave: muchas veces el precio aparece tarde; networkidle ayuda bastante
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass

            await page.wait_for_selector("span.x-currency", timeout=60000)

            prices_raw = await page.locator("span.x-currency").all_inner_texts()
            prices = [normalize_price(p) for p in prices_raw if normalize_price(p)]

            current_price = prices[-1] if prices else None
            previous_price = prices[-2] if len(prices) > 1 else None

            return {
                "isbn": isbn,
                "price_current_eur": current_price,
                "price_previous_eur": previous_price,
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
