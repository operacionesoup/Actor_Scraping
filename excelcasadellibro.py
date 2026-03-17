from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
import csv
import json
from typing import Optional, List


def normalize_price(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"(\d{1,4}(?:[.,]\d{2})?)", text)
    if not m:
        return None
    return m.group(1).replace(",", ".")


def clean_isbn(isbn: str) -> str:
    return (isbn or "").strip().replace(" ", "").replace("-", "")


def accept_cookies(page) -> None:
    patterns = [
        re.compile(r"(aceptar|acepto|agree|accept)", re.I),
    ]

    for _ in range(3):
        for pattern in patterns:
            try:
                btn = page.get_by_role("button", name=pattern).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=3000)
                    page.wait_for_timeout(700)
                    return
            except Exception:
                pass

        for sel in [
            "#onetrust-accept-btn-handler",
            'button:has-text("Aceptar")',
            'button:has-text("Aceptar cookies")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(timeout=3000)
                    page.wait_for_timeout(700)
                    return
            except Exception:
                pass


def get_first_result_card(page):
    candidate_selectors = [
        '[data-test="search-result-item"]',
        '[data-testid="search-result-item"]',
        '.product-grid-item',
        '.product-item',
        '.search-result-item',
        'article',
    ]

    for sel in candidate_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass

    return None


def scrape_casadellibro_isbn(page, isbn: str):
    isbn = clean_isbn(isbn)
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    if not isbn.isdigit() or not (10 <= len(isbn) <= 13):
        return {
            "isbn": isbn,
            "title": None,
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": url,
            "error": "ISBN inválido",
        }

    try:
        print(f"Abriendo: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        accept_cookies(page)
        page.wait_for_timeout(1500)

        try:
            page.wait_for_selector("body", timeout=5000)
        except PlaywrightTimeoutError:
            pass

        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            pass

        if "No se han encontrado resultados" in body_text or "No se encontraron resultados" in body_text:
            return {
                "isbn": isbn,
                "title": None,
                "price_current_eur": None,
                "price_previous_eur": None,
                "url": page.url,
                "error": "No se encontraron resultados",
            }

        card = None
        for _ in range(5):
            card = get_first_result_card(page)
            if card is not None:
                break
            page.wait_for_timeout(1000)

        scope = card if card is not None else page

        title = None
        title_selectors = [
            '[data-test="result-title"]',
            '[data-testid="result-title"]',
            'h2',
            'h3',
            '.title',
            '.product-title',
            'a[title]',
        ]

        for sel in title_selectors:
            try:
                loc = scope.locator(sel).first
                if loc.count() > 0:
                    txt = loc.inner_text(timeout=2000).strip()
                    if txt:
                        title = txt
                        break
                attr = loc.get_attribute("title")
                if attr and attr.strip():
                    title = attr.strip()
                    break
            except Exception:
                pass

        current_price = None
        previous_price = None

        current_price_selectors = [
            '[data-test="result-current-price"] span.x-currency',
            '[data-testid="result-current-price"] span.x-currency',
            '[data-test="result-current-price"]',
            '.price-current',
            '.price',
            'span.x-currency',
        ]

        previous_price_selectors = [
            '[data-test="result-previous-price"] span.x-currency',
            '[data-testid="result-previous-price"] span.x-currency',
            '[data-test="result-previous-price"]',
            '.price-old',
            '.old-price',
            '.price-previous',
        ]

        for sel in current_price_selectors:
            try:
                texts = scope.locator(sel).all_inner_texts()
                values = [normalize_price(t) for t in texts if normalize_price(t)]
                if values:
                    current_price = values[0]
                    break
            except Exception:
                pass

        for sel in previous_price_selectors:
            try:
                texts = scope.locator(sel).all_inner_texts()
                values = [normalize_price(t) for t in texts if normalize_price(t)]
                if values:
                    previous_price = values[0]
                    break
            except Exception:
                pass

        if current_price is None:
            try:
                all_prices = scope.locator("span.x-currency").all_inner_texts()
                values = [normalize_price(t) for t in all_prices if normalize_price(t)]
                seen = set()
                dedup = []
                for v in values:
                    if v not in seen:
                        seen.add(v)
                        dedup.append(v)

                if len(dedup) >= 1:
                    current_price = dedup[0]
                if len(dedup) >= 2:
                    previous_price = dedup[1]
            except Exception:
                pass

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
            "error": f"Timeout al procesar ISBN {isbn}",
        }


def scrape_casadellibro_batch(isbns: List[str], pause_ms: int = 1500):
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=150)
        context = browser.new_context(locale="es-ES")
        page = context.new_page()

        try:
            for i, raw_isbn in enumerate(isbns, start=1):
                isbn = clean_isbn(raw_isbn)
                if not isbn:
                    continue

                print(f"\n[{i}/{len(isbns)}] Procesando ISBN: {isbn}")
                result = scrape_casadellibro_isbn(page, isbn)
                results.append(result)

                if i < len(isbns):
                    page.wait_for_timeout(pause_ms)
        finally:
            context.close()
            browser.close()

    return results


def read_isbns_from_csv(path: str, column_name: str = "ISBN") -> List[str]:
    isbns = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            value = row.get(column_name)
            if value:
                isbns.append(value)
    return isbns


if __name__ == "__main__":
    # Opción 1: lista manual
    isbns = [
        "9788490366646",
        "9781108709767",
        "9788466837439",
    ]

    # Opción 2: leer desde CSV
    # isbns = read_isbns_from_csv("isbns.csv", column_name="ISBN")

    results = scrape_casadellibro_batch(isbns, pause_ms=2000)

    print("\nRESULTADOS FINALES:")
    print(json.dumps(results, ensure_ascii=False, indent=2))

    with open("resultados_casadellibro.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)