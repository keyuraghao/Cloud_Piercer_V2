# Cloud Pearser v2.0.0

Search [GrayHatWarfare](https://buckets.grayhatwarfare.com/) for exposed cloud storage buckets and enumerate their contents — controlled entirely from a browser dashboard.

---

## Quick Start

```bash
# 1. Install dependencies
pip install flask requests

# 2. Start the server (auto-opens browser)
python server.py

# 3. Browser opens at http://localhost:5000 — configure and click ▶ Run Scan
```

Custom port:
```bash
python server.py --port 8080
python server.py --no-browser   # don't auto-open browser
```

---

## Project Structure

```
cloud_pearser/
├── server.py                    ← Flask server + REST API (START HERE)
├── dashboard.html               ← Browser UI (served by server.py)
├── main.py                      ← CLI entry point (optional, no browser)
├── requirements.txt
├── keywords.txt                 ← Search keywords, one per line
├── File_extensions.txt          ← Allowed extensions for Azure enum
└── cloud_pearser/
    ├── config.py                ← All settings in one place
    ├── api.py                   ← GrayHatWarfare API client
    ├── parsers/
    │   ├── unique.py            ← Dedup bucket list
    │   ├── providers.py         ← AWS / Azure / GCP / DO CSVs
    │   └── azure_enum.py        ← Azure container enumeration
    └── utils/
        ├── files.py             ← Shared file helpers
        └── logger.py            ← Coloured console output
```

---

## Dashboard Features

| Section | What it does |
|---|---|
| **Overview** | Stats cards, bar chart, donut chart for all providers |
| **New Scan** | Configure API key, keywords, offsets; watch live log stream |
| **Buckets** | Browse AWS / Azure / GCP / DigitalOcean results; download CSV |
| **Azure Enum** | Extension frequency chips, top-8 bar chart, blob URL table |
| **Output Files** | Browse and download every file from any past run |
| **History** | Table of all scans with per-provider counts and status |

All scan data streams to the browser in real-time via **Server-Sent Events (SSE)**.

---

## REST API (for scripting)

| Endpoint | Method | Description |
|---|---|---|
| `GET /api/status` | GET | Current scan state + progress |
| `GET /api/config` | GET | Default config (API key, keywords) |
| `POST /api/config` | POST | Save keywords to keywords.txt |
| `POST /api/scan/start` | POST | Start a scan |
| `POST /api/scan/stop` | POST | Request stop |
| `GET /api/logs` | GET | SSE stream of live log lines |
| `GET /api/history` | GET | Session scan history |
| `GET /api/outputs` | GET | List all output directories |
| `GET /api/outputs/<ts>/files` | GET | Files in a specific run |
| `GET /api/outputs/<ts>/csv/<provider>` | GET | CSV preview (200 rows) |
| `GET /api/outputs/<ts>/urls` | GET | Azure blob URLs (100 rows) |
| `GET /api/outputs/<ts>/ext_counts` | GET | Azure file extension counts |
| `GET /api/outputs/<ts>/download/<file>` | GET | Download a file |

---

## Outputs (per scan run)

```
Outputs/
└── 2025-01-15_14-30-00_Outputs/
    ├── Unique_<ts>.txt              ← all unique bucket names
    ├── AWS_<ts>.csv                 ← S3 buckets (Bucket, URL)
    ├── Azure_<ts>.csv               ← Azure Blob (Account, URL, Container)
    ├── GCP_<ts>.csv                 ← GCS buckets (Bucket, URL)
    ├── DigitalOcean_<ts>.csv        ← DO Spaces (Bucket, URL)
    ├── URLs_<ts>.txt                ← Azure blob file URLs
    ├── Types_of_files_<ts>.txt      ← Extension frequency from Azure
    ├── summary_<ts>.json            ← Machine-readable counts
    ├── keywords.txt                 ← Copy of keywords used
    └── File_extensions.txt          ← Copy of extension whitelist
```

---

## Bug Fixes vs Original Code

| # | File | Bug | Fix |
|---|---|---|---|
| 1 | Cloud_pearser.py | Cleanup loop inside `with open()` block — only ran during write | Separate `_clean_tmp()` at startup |
| 2 | Cloud_pearser.py | Double-slash path from `os.getcwd()+"/"` | `pathlib.Path` throughout |
| 3 | Cloud_pearser.py | API `start` sent as strings `"0"`, `"1000"` | Cast to `int` |
| 4 | Cloud_pearser.py | `multiprocessing` imported, never used | Removed |
| 5 | Cloud_pearser.py | Summary read before subprocess finished | All processing in-process |
| 6 | AWS/Azure/GCP/DOS.py | `remove_words_and_characters()` used globals before definition | Function takes explicit args |
| 7 | Azure.py | Trailing-row append always ran → duplicate last CSV row | Fixed in `write_csv_3col()` |
| 8 | Azure.py | `"container"` filter matched all JSON keys | Requires Azure domain to also be present |
| 9 | Azure_Enumeration.py | CSV header row included in URL building | `next(reader, None)` skips header |
| 10 | Azure_Enumeration.py | `requests.get()` with no timeout | `AE_REQUEST_TIMEOUT` applied |
| 11 | All files | State via `/tmp` files (fragile, race-prone) | Context passed as function arguments |
