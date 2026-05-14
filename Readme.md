Live URL: `https://YOUR_APP.onrender.com`

Demo: `[Loom link]`

I looked at each sector. I found that the metrics are different because the companies do not have the same things to offer.

You can see each of the metrics in the URL that we deployed.

1. Pipeline Walkthrough

Step 1. Hunter (`app/ingest/hunter/sectorspecific.py`)

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
The `/refresh` button, in the UI calls the orchestrator.run_sector(sector) function directly.
Every run writes a row to `refresh_log` with document counts and any errors.

5. Setup & Deployment
We used Render to deploy the code and Supabash to store the data online.

6. Here is the analysis based on the imports outlined in the three extractor files in conjunction with the module listing:
`extractor.py` (US Biotech)

| `section_router.py` | `extract_relevant_text()` — focuses on MD&A/Business sections, filters chunks |

| `xbrl_client.py` | `extract_cik_from_url()` + `fetch_all_quarters_for_company()` — fetches SEC XBRL data |

| `schemas.py` | `USBiotechTimeSeriesMetrics` — defines the output schema for Pydantic |

`extractor_defence.py` (Indian Defence)

| `schemas.py` | `IndianDefenceTimeSeriesMetrics` — defines the output schema for Pydantic |

| `keywords.py` | Likely contributes to the `_ORDERBOOK_ANCHORS` regex expressions (e.g., `order book`, `order backlog`, etc.) |

`extractor_fintech.py` (Indian Fintech)

| `strategies.py` | Defines per-ticker configuration in the function `get_strategy(ticker)` (modes can be text, image, hybrid, etc.), along with anchors and target fields) |

| `page_renderer.py` | Involved in text extraction and PDF printing or PNG slide rendering via `extract_text_block()` and `render_kpi_pages()` |

| `schemas.py` | Defines the output schema for Pydantic as `IndianFintechTimeSeriesMetrics` |

| `anchor_miner.py` | Discovered slides KPI anchors, which can be utilised by `page_renderer` or `strategies` |

Common to All Three

| `schemas.py` | Each imports their time-series metrics schemas from this file |

| `fact_assembler.py` | Not directly imported, but likely creates fact blocks that are deterministic (yfinance/XBRL) utilised within the LLM in prompts — probably called in a hierarchical manner |

---

The `xbrl_client.py` file underwent **6 git changes** (makes it the most modified file), and this is understandable given the issues that the SEC XBRL API has. `extractor_fintech.py` reflects the complexity of the multi-modal + per-t










WRITE UP 
2. The most challenging aspect of my experience was the lack of access to essential API tools, such as BravsearchAPI and various language model services, which were all behind a paywall. Although I had the benefit of a free student plan with Gemini, its strict schema rules and limitations posed significant obstacles. As a result, I found myself needing to allocate my own funds to pay for LLM calls, which was not ideal. 
Moreover, I faced particular difficulties in extracting data from PDFs that were rich in visual content. The complexity of these documents made it extremely hard to chunk the text effectively, often rendering it irrelevant for integration with the language model. These hurdles were frustrating and highlighted the significant constraints imposed by the available APIs.

3. In my recent project, I utilized ChatGPT extensively for various coding tasks and smaller snippets of code that required quick solutions. For more complex planning and structuring of functions and integration into the overall codebase, I relied on Claude. In summary, I would say that around 80% of the coding work was influenced by ChatGPT, while the logical application and planning of the project accounted for about 40% of my overall efforts.
