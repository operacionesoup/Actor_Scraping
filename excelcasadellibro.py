from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import pandas as pd
import time
import re
from typing import Optional, Dict


# ---------------------------
# Utils
# ---------------------------

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
            btn = page.get_by_role(
                "button",
                name=re.compile(r"(aceptar|acepto|agree)", re.I)
            ).first
            btn.click(timeout=2500)
            page.wait_for_timeout(500)
            return
        except Exception:
            pass


def find_isbn_column(df: pd.DataFrame) -> str:
    for col in df.columns:
        if "isbn" in col.lower():
            return col
    return df.columns[0]


# ---------------------------
# Scraper Casa del Libro
# ---------------------------

def scrape_casadellibro_from_results(page, isbn: str) -> Dict:
    url = f"https://www.casadellibro.com/libros?query={isbn}"

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        accept_cookies(page)

        page.wait_for_selector("span.x-currency", timeout=20000)

        prices = page.locator("span.x-currency").all_inner_texts()
        prices = [normalize_price(p) for p in prices if normalize_price(p)]

        current_price = prices[-1] if prices else None
        previous_price = prices[-2] if len(prices) > 1 else None

        return {
            "price_current_eur": current_price,
            "price_previous_eur": previous_price,
            "url": page.url,
            "error": None
        }

    except PlaywrightTimeoutError as e:
        page.screenshot(path=f"timeout_{isbn}.png", full_page=True)
        return {
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": page.url,
            "error": f"Timeout ({isbn})"
        }

    except Exception as e:
        page.screenshot(path=f"error_{isbn}.png", full_page=True)
        return {
            "price_current_eur": None,
            "price_previous_eur": None,
            "url": page.url,
            "error": str(e)
        }


# ---------------------------
# Main Excel pipeline
# ---------------------------

def main(
    excel_path="Precios.xlsx",
    output_path="Precios_casadellibro.xlsx",
    headless=True,
    sleep_seconds=0.7,
):
    df = pd.read_excel(excel_path)

    isbn_col = find_isbn_column(df)
    print(f"Usando columna ISBN: {isbn_col}")

    df[isbn_col] = (
        df[isbn_col]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.strip()
    )

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(locale="es-ES")
        page = context.new_page()

        for i, isbn in enumerate(df[isbn_col], start=1):
            print(f"[{i}/{len(df)}] Procesando ISBN: {isbn}")

            data = scrape_casadellibro_from_results(page, isbn)

            results.append({
                "precio_actual_eur": data["price_current_eur"],
                "precio_anterior_eur": data["price_previous_eur"],
                "url_casadellibro": data["url"],
                "error_casadellibro": data["error"],
            })

            time.sleep(sleep_seconds)

        browser.close()

    result_df = pd.DataFrame(results)
    final_df = pd.concat([df.reset_index(drop=True), result_df], axis=1)

    final_df.to_excel(output_path, index=False)
    print(f"\nArchivo generado correctamente: {output_path}")


# ---------------------------
# Run
# ---------------------------

if __name__ == "__main__":
    main(
        excel_path="Precios.xlsx",
        output_path="Precios_casadellibro.xlsx",
        headless=False,   # pon True cuando ya confíes
        sleep_seconds=0.7
    )
