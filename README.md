# Cloud Pearser

Search [GrayHatWarfare](https://buckets.grayhatwarfare.com/) for exposed cloud storage buckets across AWS S3, Azure Blob, Google Cloud Storage, and DigitalOcean Spaces — controlled entirely from a browser dashboard.

---

## Features

- **Live scanning** with real-time log stream (Server-Sent Events)
- **Multiple concurrent scans** via multiprocessing — each scan runs in its own child process
- **Per-keyword results** — filter overview stats and charts by any keyword
- **Azure blob enumeration** — enumerate container contents and file types
- **Scheduled scans** — APScheduler-backed recurring scans with hour or minute intervals
- **AI bucket classification** *(optional)* — zero-shot categorization of discovered buckets via Hugging Face or OpenAI
- **Scan history** — persistent across page refreshes; view, browse, and delete past runs
- **Dark / Light theme** toggle with `localStorage` persistence
- **REST API** — all features accessible via HTTP for scripting

---

## Quick Start

**1. Clone and install dependencies**

```bash
git clone https://github.com/YOUR_USERNAME/cloud_pearser.git
cd cloud_pearser
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

**2. Set your API key**

```bash
cp .env.example .env
# Edit .env and set GRAYHATWARFARE_API_KEY=<your_key>
```

Get your API key from [buckets.grayhatwarfare.com](https://buckets.grayhatwarfare.com/).

**3. Start the server**

```bash
python server.py
# Browser opens automatically at http://localhost:5000
```

Options:
```bash
python server.py --port 8080          # custom port
python server.py --host 0.0.0.0       # listen on all interfaces
python server.py --no-browser         # skip auto-open
```

---

## Project Structure

```
cloud_pearser/
├── server.py                    ← Flask server + REST API
├── dashboard.html               ← Single-page browser UI
├── main.py                      ← CLI entry point (no browser)
├── requirements.txt
├── .env.example                 ← Copy to .env and add your API key
├── keywords.txt                 ← Search keywords, one per line
├── File_extensions.txt          ← Allowed extensions for Azure enumeration
└── cloud_pearser/
    ├── config.py                ← All settings (reads GRAYHATWARFARE_API_KEY from env)
    ├── api.py                   ← GrayHatWarfare API client
    ├── parsers/
    │   ├── unique.py            ← Deduplicate bucket list
    │   ├── providers.py         ← AWS / Azure / GCP / DO CSV generation
    │   └── azure_enum.py        ← Azure container enumeration
    └── utils/
        ├── files.py             ← Shared file helpers
        └── logger.py            ← Console output
```

---

## Dashboard Sections

| Section | Description |
|---|---|
| **Overview** | Provider stats, bar chart, donut chart; filter by keyword pill |
| **New Scan** | Configure API key, keywords, offsets; live log stream |
| **Buckets** | Browse AWS / Azure / GCP / DigitalOcean results; download CSV |
| **Azure Enum** | File extension chips, top-8 bar chart, blob URL table |
| **Output Files** | Browse and download every file from any past run |
| **History** | All past scans with per-provider counts; view or delete |
| **Schedules** | Create recurring scans (hours or minutes interval) |
| **AI Analysis** | Classify discovered buckets by category using Hugging Face or OpenAI |

---

## REST API

| Endpoint | Method | Description |
|---|---|---|
| `GET /api/status` | GET | All running scans + latest finished state |
| `GET /api/status?scan_id=X` | GET | State for a specific scan |
| `GET /api/config` | GET | Default config (keywords, offsets) |
| `POST /api/config` | POST | Save keywords to `keywords.txt` |
| `POST /api/scan/start` | POST | Start a new scan; returns `{scan_id}` |
| `POST /api/scan/stop` | POST | Stop a scan `{scan_id}` or all running scans |
| `GET /api/logs?scan_id=X` | GET | SSE stream of live log lines for a scan |
| `GET /api/history` | GET | Session scan history |
| `GET /api/outputs` | GET | List all output directories |
| `GET /api/outputs/<ts>/files` | GET | Files in a specific run |
| `GET /api/outputs/<ts>/summary` | GET | Summary JSON for a run |
| `GET /api/outputs/<ts>/csv/<provider>` | GET | CSV preview (200 rows) |
| `GET /api/outputs/<ts>/urls` | GET | Azure blob URLs (100 rows) |
| `GET /api/outputs/<ts>/ext_counts` | GET | Azure file-extension counts |
| `GET /api/outputs/<ts>/download/<file>` | GET | Download a file |
| `DELETE /api/outputs/<ts>` | DELETE | Delete a scan run and all its files |
| `GET /api/schedules` | GET | List all scheduled scans |
| `POST /api/schedules` | POST | Create a scheduled scan |
| `DELETE /api/schedules/<id>` | DELETE | Delete a schedule |
| `POST /api/schedules/<id>/toggle` | POST | Enable / pause a schedule |
| `POST /api/outputs/<ts>/ai-analyze` | POST | Run AI classification on a scan's buckets |
| `GET /api/outputs/<ts>/ai-results` | GET | Return cached AI results for a scan |

---

## Output Files (per scan run)

```
Outputs/
└── 2025-01-15_14-30-00_Outputs/
    ├── Unique_<ts>.txt              ← all unique bucket names
    ├── AWS_<ts>.csv                 ← S3 buckets (Bucket, URL)
    ├── Azure_<ts>.csv               ← Azure Blob (Account, URL, Container)
    ├── GCP_<ts>.csv                 ← GCS buckets (Bucket, URL)
    ├── DigitalOcean_<ts>.csv        ← DO Spaces (Bucket, URL)
    ├── URLs_<ts>.txt                ← Azure blob file URLs
    ├── Types_of_files_<ts>.txt      ← Extension frequency from Azure enum
    ├── summary_<ts>.json            ← Machine-readable counts + keyword breakdown
    ├── keywords.txt                 ← Copy of keywords used in this scan
    └── File_extensions.txt          ← Copy of extension allowlist used
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GRAYHATWARFARE_API_KEY` | Yes | Your GrayHatWarfare API bearer token |

Set via `.env` file (copied from `.env.example`) or exported in your shell.

---

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web server and REST API |
| `requests` | HTTP client for GrayHatWarfare API and Azure enumeration |
| `python-dotenv` | Loads `GRAYHATWARFARE_API_KEY` from `.env` |
| `APScheduler` | Recurring scheduled scans (optional — schedules tab requires it) |

---

## AI Bucket Classification (Optional)

The **AI Analysis** tab classifies discovered bucket names by industry/sensitivity using zero-shot classification. No fine-tuning required — works with any compatible model.

**Providers:**

| Provider | Default model | Notes |
|---|---|---|
| Hugging Face | `facebook/bart-large-mnli` | Free tier available; first request may be slow (model loading) |
| OpenAI | `gpt-4o-mini` | Faster, batches 10 buckets per request |

**Categories:** `finance`, `healthcare`, `government`, `technology`, `media`, `retail`, `education`, `legal`, `personal_data`, `general`

Results are cached in `ai_analysis_<ts>.json` inside the scan's output directory and reloaded automatically when you revisit the AI Analysis tab.

To use, navigate to **AI Analysis** in the sidebar, select a completed scan, choose provider, enter your API key, and click **Analyze Buckets**.

---

## Security Notes

- The server binds to `127.0.0.1` by default — only accessible from your local machine.
- Use `--host 0.0.0.0` only on trusted networks; there is no authentication layer.
- `schedules.json` and `.env` are excluded from git — they may contain your API key.
- Output files are excluded from git — they may contain sensitive discovered URLs.
