"""
config.py  –  Central configuration for Cloud Pearser.
All tuneable constants live here so nothing is scattered across files.
"""

import os
from pathlib import Path

# Load .env if present (requires python-dotenv, silently skipped if not installed)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
API_URL     = "https://buckets.grayhatwarfare.com/api/v2/files"
API_KEY     = os.environ.get("GRAYHATWARFARE_API_KEY", "")  # set in .env or environment
API_LIMIT   = 1000                                          # results per page
API_OFFSETS = [0, 1000]                                     # pagination start values
API_SLEEP   = 1.0                                           # seconds between requests
API_TIMEOUT = 30                                            # seconds per request

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
TMP_DIR      = Path("/tmp/Cloud_Outputs")
KEYWORDS_FILE = Path("keywords.txt")
EXTENSIONS_FILE = Path("File_extensions.txt")

# ---------------------------------------------------------------------------
# Cloud providers  –  name → domain substring to match in API results
# ---------------------------------------------------------------------------
PROVIDERS = {
    "AWS":          "amazonaws.com",
    "Azure":        "blob.core.windows.net",
    "GCP":          "googleapis.com",
    "DigitalOcean": "digitaloceanspaces.com",
}

# ---------------------------------------------------------------------------
# Cleaning rules applied when extracting bucket/URL values from raw JSON lines
# ---------------------------------------------------------------------------
STRIP_WORDS = ["bucket", "url", "container"]
STRIP_CHARS = [":", ",", '"', " "]

# ---------------------------------------------------------------------------
# JSON fields that are NOT bucket/URL data – used by Unique filter
# ---------------------------------------------------------------------------
UNIQUE_EXCLUDE_WORDS = {"buckets", "bucketId", "notice"}

# ---------------------------------------------------------------------------
# Azure enumeration
# ---------------------------------------------------------------------------
AZURE_CONTAINER_QUERY = "?restype=container&comp=list"
AZURE_URL_TAG_PATTERN = r"<Url>(.*?)</Url>"
AE_REQUEST_TIMEOUT    = 15  # seconds per Azure enumeration request
