from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
from typing import Optional


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")


def accept_cookies(page) -> None:
    for _ in range(3):
        try:
            page.get_by_role(
                "button",
                name=re.compile(r"(aceptar|acepto|agree)", re.I)
            ).first.click(timeout=3000)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass


def scrape_casadellibro_isbn(isbn: str):
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # IMPORTANTE
        context = browser.new_context(locale="es-ES")
        page = context.new_page()

        try:
            print(f"Abriendo: {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            accept_cookies(page)

            # en vez de esperar cualquier span.x-currency (que incluye recomendaciones),
            # esperamos específicamente el precio del resultado
            page.wait_for_selector('[data-test="result-current-price"] span.x-currency', timeout=30000)

            # coger SOLO los precios del resultado (no recomendaciones)
            current_text = None
            previous_text = None

            cur_loc = page.locator('[data-test="result-current-price"] span.x-currency').first
            if cur_loc.count() > 0:
                current_text = cur_loc.inner_text(timeout=5000)

            prev_loc = page.locator('[data-test="result-previous-price"] span.x-currency').first
            if prev_loc.count() > 0:
                previous_text = prev_loc.inner_text(timeout=2000)

            current_price = normalize_price(current_text)
            previous_price = normalize_price(previous_text)

            # si no hay precio del resultado, ya no sigas buscando más cosas
            if not current_price:
                result = {
                    "isbn": isbn,
                    "price_current_eur": None,
                    "price_previous_eur": None,
                    "url": page.url,
                }
                print("RESULTADO:")
                print(result)
                return

            result = {
                "isbn": isbn,
                "price_current_eur": current_price,      # precio final (con descuento si existe)
                "price_previous_eur": previous_price,    # precio tachado (si existe), si no queda None
                "url": page.url,
            }

            print("RESULTADO:")
            print(result)

        except PlaywrightTimeoutError as e:
            print(f"Timeout al procesar ISBN {isbn}: {e}")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    scrape_casadellibro_isbn("9788466837439")
