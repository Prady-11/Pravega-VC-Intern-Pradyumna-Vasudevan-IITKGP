"""
hunter_indian_fintech_ir.py
---------------------------
Scrapes IR documents for 7 Indian fintech companies sequentially.

Naming convention (all scrapers):
    {TICKER}.NS_{PERIOD}_{DOC_TYPE}_{MD5_8CHARS}.{EXT}

    Quarterly  →  PAYTM.NS_Q4_2025_financial_statement_2c2bbaba.xls
    Annual     →  CDSL.NS_2026_investor_presentation_ca88b629.pdf

Dedup guarantee:
    _save_if_new() is the ONLY function that writes files.
    It checks os.path.exists() before every write — no file is ever
    downloaded twice.

Run:
    python hunter_indian_fintech_ir.py

Output root:
    /Users/apple/Pravega/data/raw/
        Bajaj/
        Zaggle/
        CreditGrameen/
        Five_Star/
        PB_Fintech/
        Paytm/
        CDSL/
"""

import hashlib
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright

# ── Root output directory ─────────────────────────────────────────────────────

BASE_DIR = "/Users/apple/Pravega/data/raw"


# ── Central helpers (shared by ALL scrapers) ──────────────────────────────────

def _compute_hash(data: bytes) -> str:
    """MD5 hex digest of raw file bytes."""
    return hashlib.md5(data).hexdigest()


def _build_filename(
    ticker: str,
    period: str,
    doc_type: str,
    content_hash: str,
    ext: str = ".pdf",
) -> str:
    """
    Build canonical filename.

    Examples
    --------
    _build_filename("PAYTM.NS",      "Q4_2025", "financial_statement", "2c2bbaba...", ".xls")
    -> "PAYTM.NS_Q4_2025_financial_statement_2c2bbaba.xls"

    _build_filename("BAJAJFINSV.NS", "2026", "investor_presentation", "ca88b629...", ".pdf")
    -> "BAJAJFINSV.NS_2026_investor_presentation_ca88b629.pdf"
    """
    safe_period = re.sub(r"\s+", "_", period.strip())
    short_hash  = content_hash[:8]
    return f"{ticker}_{safe_period}_{doc_type}_{short_hash}{ext}"


def _save_if_new(
    data: bytes,
    directory: str,
    ticker: str,
    period: str,
    doc_type: str,
    ext: str = ".pdf",
) -> Optional[str]:
    """
    Hash content -> build canonical filename -> skip if already exists -> else save.

    Returns the saved filename, or None when the file was skipped.
    This is the ONLY function that writes files.
    """
    content_hash = _compute_hash(data)
    filename     = _build_filename(ticker, period, doc_type, content_hash, ext)
    save_path    = os.path.join(directory, filename)

    if os.path.exists(save_path):
        print(f"  ⏭  Already on disk — skipping : {filename}")
        return None

    with open(save_path, "wb") as fh:
        fh.write(data)

    size_kb = len(data) // 1024
    print(f"  ✅ Saved ({size_kb:,} KB) : {filename}")
    return filename


def _fy_to_start_year(fy_str: str) -> str:
    """
    Extract the START calendar year from any FY string.

    Examples
    --------
    "FY 2024-25"  -> "2024"
    "FY 2025-26"  -> "2025"
    "2024-2025"   -> "2024"
    "2025-2026"   -> "2025"
    "FY_2023-24"  -> "2023"
    """
    s = fy_str.strip()

    # "2024-2025" or "2025-2026"  — grab the first 4-digit year
    m = re.search(r"(\d{4})[-_/]\d{2,4}$", s)
    if m:
        return m.group(1)

    # Bare 4-digit year
    m = re.search(r"(\d{4})", s)
    if m:
        return m.group(1)

    return s  # unchanged fallback


def _extract_period_from_url(url: str, fallback: str) -> str:
    """
    Best-effort period extraction from a PDF/XLS URL.
    Returns Q{n}_{year} when a quarter is found, else just {year}.

    Examples
    --------
    ".../Q3FY25_Investor_Pres.pdf"  -> "Q3_2025"
    ".../FY2024-25_Report.pdf"      -> "2025"
    ".../2024-25-presentation.pdf"  -> "2025"
    Falls back to `fallback` when nothing is recognisable.
    """
    basename = os.path.basename(urlparse(url).path)

    # Q3FY25 / Q3-FY25 / Q3_FY25  — FY25 means FY 2024-25, start year = 2024
    m = re.search(r"(Q[1-4])[-_]?FY[-_]?(\d{2,4})", basename, re.IGNORECASE)
    if m:
        quarter  = m.group(1).upper()
        raw_year = m.group(2)
        end_year = ("20" + raw_year) if len(raw_year) == 2 else raw_year
        start_year = str(int(end_year) - 1)
        return f"{quarter}_{start_year}"

    # FY2024-25 / FY_2024-25 — start year is the first number
    m = re.search(r"FY[-_\s]?(\d{4})[-_](\d{2,4})", basename, re.IGNORECASE)
    if m:
        return m.group(1)   # "2024"

    # 2024-25 / 2024-2025 — start year is the first number
    m = re.search(r"(\d{4})[-_](\d{2,4})", basename)
    if m:
        return m.group(1)   # "2024"

    return fallback


# ════════════════════════════════════════════════════════════════════════════
# 1. BAJAJ FINSERV
# ════════════════════════════════════════════════════════════════════════════
def run_bajaj():
    TICKER       = "BAJAJFINSV.NS"
    DOC_TYPE     = "investor_presentation"
    DOWNLOAD_DIR = os.path.join(BASE_DIR, "Bajaj")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    YEARS = [
        "FY 2025-26",
        "FY 2024-25",
        "FY 2023-24",
        "FY 2022-23",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=1000)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        page.goto("https://www.aboutbajajfinserv.com/finance-investor-relations-investor-presentation")
        page.wait_for_timeout(5000)

        page.get_by_text("Investor Presentation").nth(0).click()
        page.wait_for_timeout(3000)

        for year in YEARS:
            print(f"\n========== {year} ==========")
            period = _fy_to_start_year(year)   # "FY 2025-26" -> "2026"

            page.locator("text=FY").nth(0).click()
            page.wait_for_timeout(2000)
            page.get_by_role("listitem").filter(has_text=year).click()
            page.wait_for_timeout(2000)

            download_buttons = page.get_by_text("DOWNLOAD")
            count = download_buttons.count()
            print(f"  Found {count} reports for {year}")

            for i in range(count):
                try:
                    print(f"  Downloading report {i + 1}/{count} ...")

                    with page.expect_download() as dl_info:
                        download_buttons.nth(i).click()

                    dl       = dl_info.value
                    _, ext   = os.path.splitext(dl.suggested_filename)
                    ext      = ext.lower() or ".pdf"

                    tmp_path = os.path.join(tempfile.gettempdir(), f"bajaj_tmp_{i}{ext}")
                    dl.save_as(tmp_path)

                    with open(tmp_path, "rb") as fh:
                        data = fh.read()
                    os.remove(tmp_path)

                    _save_if_new(data, DOWNLOAD_DIR, TICKER, period, DOC_TYPE, ext)
                    page.wait_for_timeout(2000)

                except Exception as exc:
                    print(f"  Failed report {i + 1}: {exc}")

        print("\nBajaj: all downloads completed.")
        time.sleep(5)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 2. ZAGGLE
# ════════════════════════════════════════════════════════════════════════════
def run_zaggle():
    TICKER       = "ZAGGLE.NS"
    DOC_TYPE     = "investor_presentation"
    DOWNLOAD_DIR = os.path.join(BASE_DIR, "Zaggle")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(accept_downloads=True)
        page    = context.new_page()

        print("Navigating to Zaggle IR page ...")
        base_url = "https://ir.zaggle.in/financials/"
        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        print("Expanding 'Investor Presentation' section ...")
        page.locator("summary").filter(has_text="Investor Presentation").click()
        page.wait_for_timeout(2000)

        elements = page.locator("details[open] .elementor-icon")
        count    = elements.count()
        print(f"  Found {count} PDF icons.")

        for i in range(count):
            try:
                element = elements.nth(i)
                url     = element.get_attribute("href")

                if not url:
                    url = element.evaluate(
                        "el => el.closest('a') ? el.closest('a').href : null"
                    )
                if not url:
                    print(f"  Skipping item {i + 1}: no valid URL found.")
                    continue

                absolute_url = urljoin(page.url, url)
                print(f"  Fetching: {absolute_url}")

                # e.g. Q3FY25_Investor_Presentation.pdf -> "Q3_2025"
                period   = _extract_period_from_url(absolute_url, fallback=f"doc_{i + 1}")
                response = context.request.get(absolute_url)
                data     = response.body()

                _save_if_new(data, DOWNLOAD_DIR, TICKER, period, DOC_TYPE, ".pdf")

            except Exception as exc:
                print(f"  Failed item {i + 1}: {exc}")

        print("\nZaggle: all tasks completed.")
        time.sleep(3)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 3. CREDIT ACCESS GRAMEEN
#    BUG FIX: original code pointed at fivestargroup.in (copy-paste error).
#    Corrected to CreditAccess Grameen's actual IR page.
# ════════════════════════════════════════════════════════════════════════════
def run_creditgrameen():
    TICKER       = "CREDITACC.NS"
    DOC_TYPE     = "investor_presentation"
    DOWNLOAD_DIR = os.path.join(BASE_DIR, "CreditGrameen")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(ignore_https_errors=True)
        page    = context.new_page()

        # FIXED URL (was incorrectly pointing to fivestargroup.in)
        print("Navigating to CreditAccess Grameen IR page ...")
        page.goto("https://www.creditaccessgrameen.in/investors/")
        page.wait_for_load_state("networkidle")

        try:
            page.get_by_role("link", name=re.compile("Financials", re.IGNORECASE)).first.click()
            page.get_by_role("link", name=re.compile("Investor Presentation", re.IGNORECASE)).first.click()
            page.wait_for_timeout(1500)
        except Exception:
            print("  Already on the correct section or navigation differs ...")

        dropdown         = page.get_by_role("combobox").first
        options          = dropdown.locator("option").all()
        years_to_process = []

        for opt in options:
            val  = opt.get_attribute("value")
            text = opt.inner_text().strip()
            if val:
                years_to_process.append({"value": val, "text": text})

        print(f"  Found {len(years_to_process)} financial years in dropdown.")

        for year_data in years_to_process:
            year_value = year_data["value"]
            year_text  = year_data["text"]
            period     = _fy_to_start_year(year_text)   # "FY 2024-25" -> "2025"

            print(f"\n  --- Processing {year_text} (period: {period}) ---")
            try:
                dropdown.select_option(year_value)
                page.wait_for_timeout(2000)

                accordion_closed = page.locator("text='Investor Presentation +'")
                if accordion_closed.count() > 0 and accordion_closed.first.is_visible():
                    accordion_closed.first.click()
                    page.wait_for_timeout(1000)

                pdf_links = page.locator("a[href*='.pdf']:visible")
                count     = pdf_links.count()
                print(f"  Found {count} visible PDF links for {year_text}.")

                for i in range(count):
                    link_element = pdf_links.nth(i)
                    href         = link_element.get_attribute("href")
                    if not href:
                        continue

                    absolute_url = urljoin(page.url, href)
                    response     = context.request.get(absolute_url)
                    data         = response.body()

                    _save_if_new(data, DOWNLOAD_DIR, TICKER, period, DOC_TYPE, ".pdf")

            except Exception as exc:
                print(f"  Error processing {year_text}: {exc}")

        print("\nCreditGrameen: all tasks completed.")
        time.sleep(3)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 4. FIVE STAR
# ════════════════════════════════════════════════════════════════════════════
def run_5star():
    TICKER       = "FIVESTAR.NS"
    DOC_TYPE     = "investor_presentation"
    DOWNLOAD_DIR = os.path.join(BASE_DIR, "Five_Star")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    FY_LABEL = "FY 2024-25"
    PERIOD   = _fy_to_start_year(FY_LABEL)   # -> "2025"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(ignore_https_errors=True)
        page    = context.new_page()

        print("Navigating to Five Star Group IR page ...")
        page.goto("https://fivestargroup.in/investors/")
        page.wait_for_load_state("networkidle")

        try:
            page.get_by_role("link", name=re.compile("Financials", re.IGNORECASE)).first.click()
            page.get_by_role("link", name=re.compile("Investor Presentation", re.IGNORECASE)).first.click()
            page.wait_for_timeout(1000)
        except Exception:
            pass

        print(f"  Selecting Year: {FY_LABEL} ...")
        page.get_by_role("combobox").select_option("NOTICES-FY-2024-25")
        page.wait_for_timeout(2000)

        print("  Opening 'Investor Presentation' accordion ...")
        accordion = page.get_by_role(
            "link", name=re.compile(r"Investor Presentation \+", re.IGNORECASE)
        ).first
        if accordion.is_visible():
            accordion.click()
            page.wait_for_timeout(1000)

        print("  Searching for PDF links ...")
        all_links = page.locator("a:visible")
        count     = all_links.count()

        pdf_links = []
        for i in range(count):
            link = all_links.nth(i)
            href = link.get_attribute("href")
            if href and ".pdf" in href.lower():
                pdf_links.append(link)

        print(f"  Found {len(pdf_links)} valid PDF links.")

        for i, icon in enumerate(pdf_links):
            try:
                print(f"\n  Clicking icon {i + 1}/{len(pdf_links)} ...")

                with page.expect_popup(timeout=10000) as popup_info:
                    icon.click()

                popup        = popup_info.value
                popup.wait_for_load_state()
                real_pdf_url = popup.url
                print(f"  Intercepted URL: {real_pdf_url}")
                popup.close()

                # Use URL-based period if detectable, else fall back to FY end-year
                period   = _extract_period_from_url(real_pdf_url, fallback=PERIOD)
                response = context.request.get(real_pdf_url)
                data     = response.body()

                _save_if_new(data, DOWNLOAD_DIR, TICKER, period, DOC_TYPE, ".pdf")

            except Exception as exc:
                print(f"  Failed icon {i + 1}: {exc}")

        print("\nFive Star: all tasks completed.")
        time.sleep(3)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 5. PB FINTECH
# ════════════════════════════════════════════════════════════════════════════
def run_pb():
    TICKER          = "PBFINTECH.NS"
    DOC_TYPE        = "earnings_call_deck"
    DOWNLOAD_FOLDER = os.path.join(BASE_DIR, "PB_Fintech")
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page    = browser.new_page()

        print("Opening PB Fintech IR page ...")
        page.goto("https://www.pbfintech.in/investor-relations/", timeout=60000)
        page.wait_for_load_state("networkidle")

        rows     = page.locator(".details").all()
        pdf_urls = []

        for row in rows:
            try:
                text = row.inner_text().lower()
                if "earnings call deck" in text:
                    href = row.locator("a").last.get_attribute("href")
                    if href and href.endswith(".pdf"):
                        if href.startswith("/"):
                            href = "https://www.pbfintech.in" + href
                        pdf_urls.append(href)
            except Exception as exc:
                print(f"  Skipping row: {exc}")

        browser.close()
        print(f"\n  Found {len(pdf_urls)} PDFs\n")

        for i, url in enumerate(pdf_urls, start=1):
            print(f"  [{i}/{len(pdf_urls)}] {url}")
            try:
                # e.g. Q3FY25_earnings_call.pdf -> "Q3_2025"
                period   = _extract_period_from_url(url, fallback=f"doc_{i}")
                response = requests.get(url, timeout=60)
                response.raise_for_status()

                _save_if_new(response.content, DOWNLOAD_FOLDER, TICKER, period, DOC_TYPE, ".pdf")

            except Exception as exc:
                print(f"  Failed: {exc}")

    print("\nPB Fintech: finished.")


# ════════════════════════════════════════════════════════════════════════════
# 6. PAYTM
# ════════════════════════════════════════════════════════════════════════════
def run_paytm():
    TICKER       = "PAYTM.NS"
    DOC_TYPE     = "financial_statement"
    DOWNLOAD_DIR = Path(os.path.join(BASE_DIR, "Paytm"))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    URL = "https://ir.paytm.com/financial-results"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page    = browser.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        xls_links = page.get_by_role("link", name="XLS")
        total     = xls_links.count()
        print(f"  Total XLS links found: {total}")

        for i in range(total):
            try:
                link = xls_links.nth(i)
                href = link.get_attribute("href")
                if not href:
                    continue

                # Start calendar year of each FY
                # First 4 links -> FY 2025-26 (starts 2025)
                # Next  4 links -> FY 2024-25 (starts 2024)
                start_year = "2025" if i < 4 else "2024"

                # Page order: Q4 Q3 Q2 Q1
                quarter_map = {0: "Q4", 1: "Q3", 2: "Q2", 3: "Q1"}
                quarter     = quarter_map[i % 4]
                period      = f"{quarter}_{start_year}"   # -> "Q4_2024"

                if href.startswith("/"):
                    href = "https://ir.paytm.com" + href

                print(f"\n  Downloading {period} ...")
                response = requests.get(href, timeout=60)
                response.raise_for_status()

                _save_if_new(
                    response.content,
                    str(DOWNLOAD_DIR),
                    TICKER,
                    period,
                    DOC_TYPE,
                    ".xls",
                )

            except Exception as exc:
                print(f"  ERROR on link {i}: {exc}")

        browser.close()

    print("\nPaytm: finished.")


# ════════════════════════════════════════════════════════════════════════════
# 7. CDSL
# ════════════════════════════════════════════════════════════════════════════
def run_cdsl():
    TICKER       = "CDSL.NS"
    DOC_TYPE     = "investor_presentation"
    DOWNLOAD_DIR = Path(os.path.join(BASE_DIR, "CDSL"))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    URL = "https://www.cdslindia.com/InvestorRels/Financial.html"

    YEARS = [
        "2025-2026",
        "2024-2025",
        "2023-2024",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page    = browser.new_page()

        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        for year in YEARS:
            print(f"\n========== {year} ==========")
            period = _fy_to_start_year(year)   # "2025-2026" -> "2026"

            page.locator("#select-year2").select_option(year)
            page.wait_for_timeout(5000)

            links = page.locator("a[href$='.pdf']")
            count = links.count()
            print(f"  PDF links found: {count}")

            for i in range(count):
                try:
                    link = links.nth(i)
                    href = link.get_attribute("href")
                    text = link.inner_text().strip()

                    if not href:
                        continue
                    if "presentation" not in text.lower():
                        continue

                    print(f"\n  FOUND: {text}")
                    href     = urljoin(URL, href)   # make absolute
                    response = requests.get(href, timeout=60)
                    response.raise_for_status()

                    _save_if_new(
                        response.content,
                        str(DOWNLOAD_DIR),
                        TICKER,
                        period,
                        DOC_TYPE,
                        ".pdf",
                    )

                except Exception as exc:
                    print(f"  ERROR: {exc}")

        browser.close()

    print("\nCDSL: finished.")


# ════════════════════════════════════════════════════════════════════════════
# MAIN — run all 7 sequentially; isolate failures per scraper
# ════════════════════════════════════════════════════════════════════════════
SCRAPERS = [
    ("Bajaj Finserv",        run_bajaj),
    ("Zaggle",               run_zaggle),
    ("CreditAccess Grameen", run_creditgrameen),
    ("Five Star",            run_5star),
    ("PB Fintech",           run_pb),
    ("Paytm",                run_paytm),
    ("CDSL",                 run_cdsl),
]


def hunter_indian_fintech_ir():
    os.makedirs(BASE_DIR, exist_ok=True)
    print(f"\nOutput root : {BASE_DIR}")
    print(f"Running {len(SCRAPERS)} scrapers sequentially.\n")

    results = []
    for name, fn in SCRAPERS:
        print(f"\n{'=' * 70}")
        print(f"▶  {name}")
        print(f"{'=' * 70}")
        try:
            fn()
            results.append((name, "OK"))
            print(f"\n✅  {name} finished.")
        except Exception as exc:
            results.append((name, f"FAILED: {exc}"))
            print(f"\n❌  {name} crashed: {exc}")

    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    for name, status in results:
        icon = "✅" if status == "OK" else "❌"
        print(f"  {icon}  {name:<25}  {status}")
    print(f"\nAll downloads under: {BASE_DIR}")
    print("Ready for parsing.\n")


if __name__ == "__main__":
    hunter_indian_fintech_ir()