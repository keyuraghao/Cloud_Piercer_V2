"""
config.py  –  Central configuration for Cloud Pearser.
All tuneable constants live here so nothing is scattered across files.
"""

import os
import sys
import tempfile
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
# Dashboard / server authentication
# ---------------------------------------------------------------------------
DASHBOARD_SECRET = os.environ.get("DASHBOARD_SECRET", "")

# Comma-separated allowed origins for CORS (empty = same-origin only)
ALLOWED_ORIGINS_RAW = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS: list[str] = (
    [o.strip() for o in ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
    if ALLOWED_ORIGINS_RAW.strip()
    else []
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_FILE  = os.environ.get("LOG_FILE", "")       # empty = stdout only

# ---------------------------------------------------------------------------
# AI API keys (loaded from env; never sent from the browser)
# ---------------------------------------------------------------------------
HUGGINGFACE_API_KEY = os.environ.get("HUGGINGFACE_API_KEY", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")

# ---------------------------------------------------------------------------
# Paths  –  cross-platform
# ---------------------------------------------------------------------------
TMP_DIR         = Path(tempfile.gettempdir()) / "Cloud_Outputs"
KEYWORDS_FILE   = Path("keywords.txt")
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


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------
def validate_env() -> list[str]:
    """Return a list of warning strings for missing/misconfigured env vars."""
    warnings: list[str] = []
    if not API_KEY:
        warnings.append(
            "GRAYHATWARFARE_API_KEY is not set. "
            "Copy .env.example → .env and add your API key, "
            "or export GRAYHATWARFARE_API_KEY=<your_key>"
        )
    if not DASHBOARD_SECRET:
        warnings.append(
            "DASHBOARD_SECRET is not set. "
            "The dashboard is unauthenticated — set a secret for any "
            "non-localhost deployment."
        )
    return warnings
