Live URL: `https://YOUR_APP.onrender.com`

Demo: `[Loom link]`

I looked at each sector. I found that the metrics are different because the companies do not have the same things to offer.

You can see each of the metrics in the URL that we deployed.

1. Pipeline Walkthrough
Step 1. Hunter** (`app/ingest/hunter/sectorspecific.py`)

For US companies, the Hunter uses the SEC EDGAR submissions API to get a list of filings. It looks for 10-Q, 10-K and 8-K types within the target date range. Then it gets the document URL. Writes a row to `documents` with `parse_status=pending`. If the URL is already in the table the filing is skipped.

For companies the Hunter uses the BSE/NSE filing search endpoints to get the filings. Then it gets the PDF links. Downloads them.

To make sure that the company IR is present for the Fintech companies I used Playwright to navigate to the IR Pages and get the PDFs and parse them.

Step 2. Postman(`app/ingest/postman.py`)

This step downloads the documents using `httpx`. PDFs are saved as bytes and HTML pages are saved as HTML. If the download is successful `parse_status` is updated to `fetched`. If there is a network failure `parse_status` is updated to `failed`.

Step 3. Reader (`app/ingest/reader.py` + `chunker.py`)

PDFs are parsed using `pdfplumber`. HTML is cleaned using `BeautifulSoup`. The text is then chunked into pieces. Chunks are stored in memory for the step.

Step 4. Extracter(`app/extract`)

1. For US Biotech Sector and Indian Defence we just parsed the PDFs. Then we used a keyword finder to get the metrics from the text.

2. For Fintech since the operational metrics were in PDFs with a lot of images we used PyMUPdf to render the images. Then we used regex to filter the metrics.


2. Synthesis Engine
Call 1. Sector synthesis: We computed the QoQ and YoY deltas cross-company dispersion and statistical outliers from the `metrics` table. Then we fed these signals to Sonnet as the input.
Call 2. Investing lens:** We parsed the reports and extracted specific information from the MD&A section.
Both outputs are written to the `synthesis` table with a `generated_at` timestamp.

3. Extraction & Validation

We have three validation layers to protect the quality:
Layer 1. Pydantic type enforcement**
Each sector has a dedicated Pydantic model. Fields have typed constraints enforced at parse time.
Layer 2. Field-level business validators**
We have custom validators to catch domain values.
Layer 3. Cross-row sanity checks**
After insertion we compare each metric against the prior period. If any metric moves more than 5× QoQ the row is flagged `needs_review`.

4. Refresh Scheduler
We use APScheduler to run the refresh pipeline.

```Python

scheduler = BackgroundScheduler()

scheduler.add_job(

orchestrator.run_all_sectors

trigger=CronTrigger(day_of_week='sun' hour=2)

id='weekly_refresh'

)

scheduler.start()

```
The `/refresh` button, in the UI calls the orchestrator.run_sector(sector)` function directly.
Every run writes a row to `refresh_log` with document counts and any errors.

5. Setup & Deployment
We used Render to deploy the code and Supabash to store the data online.
