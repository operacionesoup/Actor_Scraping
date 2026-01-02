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

            page.wait_for_selector("span.x-currency", timeout=20000)

            prices_raw = page.locator("span.x-currency").all_inner_texts()
            prices = [normalize_price(p) for p in prices_raw if normalize_price(p)]

            current_price = prices[-1] if prices else None
            previous_price = prices[-2] if len(prices) > 1 else None

            result = {
                "isbn": isbn,
                "price_current_eur": current_price,
                "price_previous_eur": previous_price,
                "url": page.url,
            }

            print("RESULTADO:")
            print(result)

        except PlaywrightTimeoutError:
            print(f"Timeout al procesar ISBN {isbn}")

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    scrape_casadellibro_isbn("9788490366646")
