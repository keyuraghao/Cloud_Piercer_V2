"""
api.py  –  GrayHatWarfare API client.

Responsible only for fetching raw JSON from the API and persisting it.
All parsing / filtering happens in the parser layer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from .config import API_KEY, API_LIMIT, API_OFFSETS, API_SLEEP, API_TIMEOUT, API_URL
from .utils import logger


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_all_keywords(keywords: list[str], tmp_dir: Path) -> dict[str, Any]:
    """
    Query the GrayHatWarfare API for every keyword × offset combination.

    Returns the combined result dict and also writes it to
    ``<tmp_dir>/output_data.json`` for caching / debugging.

    Bug fixed from original: start values were sent as strings ("0", "1000").
    The API expects integers; passing strings meant pagination silently failed.
    """
    if not keywords:
        raise ValueError("No keywords provided.")

    headers = {"Authorization": f"Bearer {API_KEY}"}
    output: dict[str, Any] = {}

    total_calls = len(keywords) * len(API_OFFSETS)
    call_n = 0

    logger.section("Fetching data from GrayHatWarfare API")

    for keyword in keywords:
        kw_data: dict[str, Any] = {}

        for offset in API_OFFSETS:
            call_n += 1
            logger.step(call_n, total_calls, f"keyword='{keyword}'  offset={offset}")

            params = {"keywords": keyword, "start": int(offset), "limit": API_LIMIT}

            try:
                resp = requests.get(
                    API_URL, headers=headers, params=params, timeout=API_TIMEOUT
                )

                if resp.status_code == 200:
                    kw_data[f"start{offset}"] = resp.json()
                elif resp.status_code == 401:
                    logger.error("API key rejected (HTTP 401). Check your API_KEY in config.py.")
                    raise SystemExit(1)
                elif resp.status_code == 429:
                    logger.warn("Rate-limited (HTTP 429). Sleeping 10 s …")
                    time.sleep(10)
                else:
                    logger.warn(
                        f"keyword='{keyword}' offset={offset}: HTTP {resp.status_code}"
                    )

            except requests.Timeout:
                logger.warn(f"Timeout for keyword='{keyword}' offset={offset}. Skipping.")
            except requests.RequestException as exc:
                logger.error(f"Network error for keyword='{keyword}': {exc}")

            time.sleep(API_SLEEP)

        output[keyword] = kw_data

    # Persist raw JSON for debugging / re-runs
    json_path = tmp_dir / "output_data.json"
    json_path.write_text(json.dumps(output, indent=4))
    logger.ok(f"Raw API data cached → {json_path}")

    return output


def load_cached(tmp_dir: Path) -> dict[str, Any] | None:
    """Return previously cached API data, or None if it does not exist."""
    json_path = tmp_dir / "output_data.json"
    if json_path.exists():
        return json.loads(json_path.read_text())
    return None


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

def flatten_to_lines(raw: dict[str, Any], dst: Path) -> list[str]:
    """
    Flatten the nested API JSON into a single list of stripped lines and
    write them to *dst*.

    This mirrors what the original code did (json → txt → strip whitespace)
    but does it in one pass instead of three.
    """
    json_str = json.dumps(raw, indent=4)
    lines = [ln.strip() for ln in json_str.splitlines()]
    dst.write_text("\n".join(lines) + "\n")
    logger.ok(f"Flattened API response → {dst.name}  ({len(lines):,} lines)")
    return lines
