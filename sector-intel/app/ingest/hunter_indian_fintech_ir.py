"""
combined_ir_scraper.py
----------------------
Runs all 7 individual IR scrapers sequentially in a single process.

Logic from each original file is preserved verbatim — only the
DOWNLOAD_DIR has been pointed at /Users/apple/Pravega/<Company>/ so that
downstream parsing can pick up files by company folder.

Folder layout after a run:
    /Users/apple/Pravega/
        Bajaj/
        Zaggle/
        CreditGrameen/
        Five_Star/
        PB_Fintech/
        Paytm/
        CDSL/

Run:
    python combined_ir_scraper.py
"""

import os
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from playwright.sync_api import sync_playwright


# Single root for everything
BASE_DIR = "/Users/apple/Pravega"


# ════════════════════════════════════════════════════════════════════════════
# 1. BAJAJ FINSERV   (from scraper_Bajaj.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_bajaj():
    DOWNLOAD_DIR = f"{BASE_DIR}/Bajaj"

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    YEARS = [
        "FY 2025-26",
        "FY 2024-25",
        "FY 2023-24"
    ]

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=False,
            slow_mo=1000
        )

        context = browser.new_context(
            accept_downloads=True
        )

        page = context.new_page()

        page.goto("https://www.aboutbajajfinserv.com/finance-investor-relations-investor-presentation")

        # Wait for page load
        page.wait_for_timeout(5000)

        # Go to Investor Presentation section
        page.get_by_text("Investor Presentation").nth(0).click()

        page.wait_for_timeout(3000)

        for year in YEARS:

            print(f"\n========== {year} ==========")

            # Open dropdown
           # Open dropdown
            page.locator("text=FY").nth(0).click()

            page.wait_for_timeout(2000)

            # Select year
            page.get_by_role("listitem").filter(
                has_text=year
            ).click()

            # Find all DOWNLOAD buttons
            download_buttons = page.get_by_text("DOWNLOAD")

            count = download_buttons.count()

            print(f"Found {count} reports")

            for i in range(count):

                try:

                    print(f"Downloading report {i+1}")

                    with page.expect_download() as download_info:

                        download_buttons.nth(i).click()

                    download = download_info.value

                    suggested_name = download.suggested_filename

                    save_path = os.path.join(
                        DOWNLOAD_DIR,
                        f"{year}_{i+1}_{suggested_name}"
                    )

                    download.save_as(save_path)

                    print(f"Saved: {save_path}")

                    page.wait_for_timeout(2000)

                except Exception as e:

                    print(f"Failed report {i+1}")
                    print(e)

        print("\nAll downloads completed")

        time.sleep(10)

        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 2. ZAGGLE   (from scraper_final_Zaggle.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_zaggle():
    DOWNLOAD_DIR = f"{BASE_DIR}/Zaggle"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print("Navigating to Zaggle IR page...")
        base_url = "https://ir.zaggle.in/financials/"
        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        # 1. Open the accordion
        print("Expanding 'Investor Presentation' section...")
        page.locator("summary").filter(has_text="Investor Presentation").click()
        page.wait_for_timeout(2000)

        # 2. Find all Elementor icons inside the OPEN accordion section
        elements = page.locator("details[open] .elementor-icon")
        count = elements.count()
        print(f"Found {count} PDF icons.")

        # 3. Iterate, extract the URL, make it absolute, and download
        for i in range(count):
            try:
                element = elements.nth(i)
                url = element.get_attribute("href")

                if not url:
                    url = element.evaluate("el => el.closest('a') ? el.closest('a').href : null")

                if not url:
                    print(f"Skipping item {i+1}: Could not find a valid URL.")
                    continue

                # --- THE MAGIC FIX IS HERE ---
                # This turns '/wp-content/...' into 'https://ir.zaggle.in/wp-content/...'
                absolute_url = urljoin(page.url, url)
                # -----------------------------

                print(f"\nDownloading from: {absolute_url}")

                parsed_url = urlparse(absolute_url)
                file_name = os.path.basename(parsed_url.path)

                if not file_name.endswith('.pdf'):
                    file_name = f"Investor_Presentation_{i+1}.pdf"

                save_path = os.path.join(DOWNLOAD_DIR, file_name)

                # Request the absolute URL
                response = context.request.get(absolute_url)

                with open(save_path, 'wb') as f:
                    f.write(response.body())

                print(f"✅ Successfully saved: {file_name}")

            except Exception as e:
                print(f"❌ Failed to process item {i+1}: {e}")

        print("\nAll tasks completed.")
        time.sleep(3)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 3. CREDIT GRAMEEN   (from scraper_creditgrameen.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_creditgrameen():
    DOWNLOAD_DIR = f"{BASE_DIR}/CreditGrameen"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        # Bypass any SSL certificate errors just like before
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        print("Navigating to Five Star Group IR page...")
        page.goto("https://fivestargroup.in/investors/")
        page.wait_for_load_state("networkidle")

        # 1. Navigate via the sidebar (borrowing from your Codegen!)
        try:
            # We use re.compile to ignore exact matches and just find the words
            page.get_by_role("link", name=re.compile("Financials", re.IGNORECASE)).first.click()
            page.get_by_role("link", name=re.compile("Investor Presentation", re.IGNORECASE)).first.click()
            page.wait_for_timeout(1500)
        except Exception:
            print("Already on the correct section...")

        # 2. Find the Dropdown Combobox and get all the years dynamically
        dropdown = page.get_by_role("combobox").first
        options = dropdown.locator("option").all()

        years_to_process = []
        for opt in options:
            val = opt.get_attribute("value")
            text = opt.inner_text().strip()
            if val:  # Skip any empty placeholders
                years_to_process.append({"value": val, "text": text})

        print(f"Found {len(years_to_process)} financial years in the dropdown.")

        # 3. Loop through every year in the dropdown
        for year_data in years_to_process:
            year_value = year_data["value"]  # e.g., "NOTICES-FY-2024-25"
            year_text = year_data["text"]    # e.g., "FY 2024-25"

            # Make a safe string for our filenames (removes spaces and slashes)
            safe_year_name = year_text.replace(" ", "_").replace("/", "-")

            print(f"\n--- Processing {year_text} ---")

            try:
                # Select the year from the dropdown
                dropdown.select_option(year_value)

                # Wait for the website to fetch the new data for that year
                page.wait_for_timeout(2000)

                # 4. Open the Accordion
                # If it says "+", it's closed, so we click it.
                accordion_closed = page.locator("text='Investor Presentation +'")
                if accordion_closed.count() > 0 and accordion_closed.first.is_visible():
                    accordion_closed.first.click()
                    page.wait_for_timeout(1000)  # Wait for animation to open

                # 5. Find the PDF Links and Download Directly
                # Look for any visible link containing ".pdf" in the URL
                pdf_links = page.locator("a[href*='.pdf']:visible")
                count = pdf_links.count()

                print(f"Found {count} visible PDF links for {year_text}.")

                for i in range(count):
                    link_element = pdf_links.nth(i)
                    href = link_element.get_attribute("href")

                    if not href:
                        continue

                    absolute_url = urljoin(page.url, href)

                    # Directly download the file via API (bypassing the popup tab!)
                    response = context.request.get(absolute_url)
                    file_data = response.body()

                    # Clean up the filename
                    parsed_url = urlparse(absolute_url)
                    original_name = os.path.basename(parsed_url.path)
                    if not original_name.endswith('.pdf'):
                        original_name = f"presentation_{i+1}.pdf"

                    safe_file_name = f"{safe_year_name}_{original_name}"
                    save_path = os.path.join(DOWNLOAD_DIR, safe_file_name)

                    # Save it to the Desktop folder
                    with open(save_path, 'wb') as f:
                        f.write(file_data)

                    # Check file size to confirm it downloaded correctly
                    file_size_kb = len(file_data) // 1024
                    print(f"✅ Saved: {safe_file_name} ({file_size_kb} KB)")

            except Exception as e:
                print(f"❌ Error processing year {year_text}: {e}")

        print("\nAll tasks completed.")
        time.sleep(3)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 4. FIVE STAR   (from scraper_5star.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_5star():
    DOWNLOAD_DIR = f"{BASE_DIR}/Five_Star"
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=500)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        print("Navigating to Five Star Group IR page...")
        page.goto("https://fivestargroup.in/investors/")
        page.wait_for_load_state("networkidle")

        # 1. Navigate to the right section (borrowing from your codegen)
        try:
            page.get_by_role("link", name=re.compile("Financials", re.IGNORECASE)).first.click()
            page.get_by_role("link", name=re.compile("Investor Presentation", re.IGNORECASE)).first.click()
            page.wait_for_timeout(1000)
        except:
            pass  # If it's already open, keep going

        # 2. Select the specific Year
        print("Selecting Year: FY 2024-25...")
        page.get_by_role("combobox").select_option("NOTICES-FY-2024-25")
        page.wait_for_timeout(2000)

        # 3. Open the Accordion
        print("Opening 'Investor Presentation +' Accordion...")
        accordion = page.get_by_role("link", name=re.compile("Investor Presentation \+", re.IGNORECASE)).first
        if accordion.is_visible():
            accordion.click()
            page.wait_for_timeout(1000)

        # 4. Find the specific PDF icons (Using your Codegen's logic for empty text links)
        print("Searching for PDF icons...")
        # Get all visible links. We will filter them manually to be safe.
        all_links = page.locator("a:visible")
        count = all_links.count()

        pdf_icons = []
        for i in range(count):
            link = all_links.nth(i)
            href = link.get_attribute("href")
            # Ensure we only target links that actually point to a PDF file
            if href and ".pdf" in href.lower():
                pdf_icons.append(link)

        print(f"Found {len(pdf_icons)} valid PDF icons.")

        # 5. Click, Intercept the Popup, and Download
        for i, icon in enumerate(pdf_icons):
            try:
                print(f"\nClicking icon {i+1}...")

                # Using YOUR expect_popup method to simulate a real human click
                with page.expect_popup(timeout=10000) as popup_info:
                    icon.click()

                popup = popup_info.value
                popup.wait_for_load_state()

                # The URL of the new tab IS the raw, unblocked PDF link!
                real_pdf_url = popup.url
                print(f"Intercepted URL: {real_pdf_url}")

                # Close the popup tab so your browser doesn't get cluttered
                popup.close()

                # Download the file using the browser context (which shares the security cookies)
                response = context.request.get(real_pdf_url)
                file_data = response.body()

                # Create a clean file name
                file_name = f"FY24-25_Presentation_{i+1}.pdf"
                save_path = os.path.join(DOWNLOAD_DIR, file_name)

                # Save the actual PDF data
                with open(save_path, 'wb') as f:
                    f.write(file_data)

                # SANITY CHECK: Print the file size.
                # If it says 2 KB, we failed. If it says 1500+ KB, we won!
                file_size_kb = len(file_data) // 1024
                print(f"✅ Saved: {file_name} ({file_size_kb} KB)")

            except Exception as e:
                print(f"❌ Failed on item {i+1}: {e}")

        print("\nAll tasks completed.")
        time.sleep(3)
        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 5. PB FINTECH   (from scraper_pb.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_pb():
    DOWNLOAD_FOLDER = f"{BASE_DIR}/PB_Fintech"
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

    def download_file(url, folder):

        filename = url.split("/")[-1]
        filepath = os.path.join(folder, filename)

        if os.path.exists(filepath):
            print(f"Already exists -> {filename}")
            return

        response = requests.get(url, timeout=60)

        with open(filepath, "wb") as f:
            f.write(response.content)

        print(f"Downloaded -> {filename}")

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page()

        print("Opening page...")

        page.goto(
            "https://www.pbfintech.in/investor-relations/",
            timeout=60000
        )

        page.wait_for_load_state("networkidle")

        rows = page.locator(".details").all()

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

            except Exception as e:
                print("Skipping row:", e)

        browser.close()

        print(f"\nFound {len(pdf_urls)} PDFs\n")

        for i, url in enumerate(pdf_urls, start=1):

            print(f"[{i}/{len(pdf_urls)}]")

            try:
                download_file(url, DOWNLOAD_FOLDER)

            except Exception as e:
                print("Failed:", e)

        print("\nFinished")


# ════════════════════════════════════════════════════════════════════════════
# 6. PAYTM   (from scraper_paytm.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_paytm():
    URL = "https://ir.paytm.com/financial-results"

    DOWNLOAD_DIR = Path(f"{BASE_DIR}/Paytm")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    def sanitize(text):
        return "".join(
            c if c.isalnum() or c in (" ", "-", "_")
            else "_"
            for c in text
        )

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page()

        page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(5000)

        xls_links = page.get_by_role("link", name="XLS")

        total = xls_links.count()

        print(f"Total XLS links: {total}")

        current_fy = None

        for i in range(total):

            try:

                link = xls_links.nth(i)

                href = link.get_attribute("href")

                if not href:
                    continue

                # ROW text ONLY
                row = link.locator("xpath=ancestor::div[1]")

                row_text = row.inner_text(timeout=2000)

                print("\n===================")
                print("LINK:", i)
                print(row_text)

                # FY mapping based on index
                # First 4 XLS => FY2026
                # Next 4 XLS => FY2025

                if i < 4:
                    fy = "FY_2025_26"
                else:
                    fy = "FY_2024_25"

                # Quarter mapping
                # Order on page is Q4 Q3 Q2 Q1

                q_index = i % 4

                quarter_map = {
                    0: "Q4",
                    1: "Q3",
                    2: "Q2",
                    3: "Q1",
                }

                quarter = quarter_map[q_index]

                filename = f"{fy}_{quarter}.xls"

                print("Downloading:", filename)

                if href.startswith("/"):
                    href = "https://ir.paytm.com" + href

                response = requests.get(href)

                save_path = DOWNLOAD_DIR / filename

                with open(save_path, "wb") as f:
                    f.write(response.content)

                print("Saved ->", save_path)

            except Exception as e:
                print("ERROR:", e)

        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# 7. CDSL   (from scraper_cdsl.py — logic untouched)
# ════════════════════════════════════════════════════════════════════════════
def run_cdsl():
    URL = "https://www.cdslindia.com/InvestorRels/Financial.html"

    DOWNLOAD_DIR = Path(f"{BASE_DIR}/CDSL")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    YEARS = [
        "2025-2026",
        "2024-2025",
        "2023-2024",
    ]

    def sanitize(text):
        return "".join(
            c if c.isalnum() or c in (" ", "-", "_")
            else "_"
            for c in text
        )

    with sync_playwright() as p:

        browser = p.chromium.launch(headless=False)

        page = browser.new_page()

        page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(5000)

        for year in YEARS:

            print(f"\n========== {year} ==========")

            # Change year
            page.locator("#select-year2").select_option(year)

            page.wait_for_timeout(5000)

            # REAL downloadable links
            links = page.locator("a[href$='.pdf']")

            count = links.count()

            print(f"PDF links found: {count}")

            for i in range(count):

                try:

                    link = links.nth(i)

                    href = link.get_attribute("href")

                    text = link.inner_text().strip()

                    if not href:
                        continue

                    if "presentation" not in text.lower():
                        continue

                    print("\nFOUND:")
                    print(text)
                    print(href)

                    # Absolute URL
                    from urllib.parse import urljoin

                    href = urljoin(URL, href)
                    filename = sanitize(
                        f"{year}_{text}.pdf"
                    )

                    save_path = DOWNLOAD_DIR / filename

                    response = requests.get(href)

                    with open(save_path, "wb") as f:
                        f.write(response.content)

                    print("Saved ->", save_path)

                except Exception as e:
                    print("ERROR:", e)

        browser.close()


# ════════════════════════════════════════════════════════════════════════════
# MAIN — run all 7 sequentially, isolate failures per scraper
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


def main():
    os.makedirs(BASE_DIR, exist_ok=True)
    print(f"\nOutput root: {BASE_DIR}")
    print(f"Running {len(SCRAPERS)} scrapers sequentially.\n")

    results = []
    for name, fn in SCRAPERS:
        print(f"\n{'='*70}")
        print(f"▶  {name}")
        print(f"{'='*70}")
        try:
            fn()
            results.append((name, "OK"))
            print(f"\n✅ {name} finished.")
        except Exception as e:
            results.append((name, f"FAILED: {e}"))
            print(f"\n❌ {name} crashed: {e}")

    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    for name, status in results:
        print(f"  {name:<25} {status}")
    print(f"\nAll downloads under: {BASE_DIR}")
    print("Ready for parsing.\n")


if __name__ == "__main__":
    main()