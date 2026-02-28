"""
parsers/azure_enum.py  –  Enumerate publicly accessible Azure Blob containers.

Ported from Azure_Enumeration.py with the following fixes / improvements:

  BUG 1 (original): AE_1.txt was built by iterating all rows including the
                     CSV header row ("Column 1", "Column 2", "Column 3").
                     This produced a malformed URL like
                     https://Column 1/Column 3?restype=container&comp=list
                     on the very first iteration.  Fixed by skipping the
                     header row with next(csv_reader).

  BUG 2 (original): requests.get(url) had no timeout.  A single hung
                     connection would block the entire enumeration indefinitely.
                     Fixed: uses AE_REQUEST_TIMEOUT.

  BUG 3 (original): The URL dedup step wrote results to AE_2.txt but the
                     set iteration order is non-deterministic, making output
                     non-reproducible across runs.  Fixed: sorted output.

  BUG 4 (original): TS was read from time_stamp.txt three separate times
                     inside the same function scope; the value cannot change
                     mid-run so this was just redundant I/O.
"""

from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..config import (
    AE_REQUEST_TIMEOUT,
    AZURE_CONTAINER_QUERY,
    AZURE_URL_TAG_PATTERN,
)
from ..utils.files import deduplicate_lines, write_lines
from ..utils import logger


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enumerate_azure(
    azure_csv: Path,
    ext_file: Path,
    output_dir: Path,
    timestamp: str,
    tmp_dir: Path,
) -> dict[str, int]:
    """
    Full Azure enumeration pipeline:

    1. Build container-listing URLs from the Azure CSV.
    2. Deduplicate those URLs.
    3. Request each URL and collect the XML response.
    4. Extract <Url> tags from the responses.
    5. Deduplicate the extracted URLs.
    6. Count file extensions against the allowed list.

    Returns ``{"url_count": N, "ext_counts": {ext: count, …}}``.
    """
    logger.section("Azure Enumeration")

    # ── Stage 1: build container listing URLs ─────────────────────────────
    query_urls = _build_query_urls(azure_csv)
    if not query_urls:
        logger.warn("No Azure rows to enumerate – skipping.")
        return {"url_count": 0, "ext_counts": {}}

    logger.info(f"Container listing URLs built: {len(query_urls):,}")

    # ── Stage 2: deduplicate ───────────────────────────────────────────────
    unique_query_urls = deduplicate_lines(query_urls)
    unique_query_urls.sort()  # deterministic order
    logger.info(f"After dedup: {len(unique_query_urls):,}")

    ae2 = tmp_dir / "AE_2.txt"
    write_lines(ae2, unique_query_urls)

    # ── Stage 3: fetch each URL ────────────────────────────────────────────
    ae3 = tmp_dir / "AE_3.txt"
    _fetch_container_listings(unique_query_urls, ae3)

    # ── Stage 4: extract <Url> tags ───────────────────────────────────────
    ae4 = tmp_dir / "AE_4.txt"
    blob_urls = _extract_blob_urls(ae3)
    write_lines(ae4, blob_urls)
    logger.info(f"Blob URLs extracted: {len(blob_urls):,}")

    # ── Stage 5: deduplicate + save ────────────────────────────────────────
    unique_blob_urls = deduplicate_lines(blob_urls)
    unique_blob_urls.sort()
    url_out = output_dir / f"URLs_{timestamp}.txt"
    write_lines(url_out, unique_blob_urls)
    logger.ok(f"Unique blob URLs: {len(unique_blob_urls):,}  →  {url_out.name}")

    # ── Stage 6: count file extensions ────────────────────────────────────
    ext_counts = _count_extensions(unique_blob_urls, ext_file)
    types_out = output_dir / f"Types_of_files_{timestamp}.txt"
    _write_ext_counts(ext_counts, types_out)
    logger.ok(f"Extension counts written  →  {types_out.name}")

    return {"url_count": len(unique_blob_urls), "ext_counts": dict(ext_counts)}


# ---------------------------------------------------------------------------
# Internal stages
# ---------------------------------------------------------------------------

def _build_query_urls(azure_csv: Path) -> list[str]:
    """
    Read the Azure CSV and build container-listing query URLs.

    Format: https://<storage_account>/<container>?restype=container&comp=list

    Column layout written by parsers/providers.py:
      col 0 → storage account  (e.g. myaccount.blob.core.windows.net)
      col 1 → full URL
      col 2 → container name
    """
    query_urls: list[str] = []
    with azure_csv.open(newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # BUG FIX: skip header row
        for row in reader:
            if len(row) >= 3 and row[0].strip() and row[2].strip():
                storage = row[0].strip()
                container = row[2].strip()
                url = f"https://{storage}/{container}{AZURE_CONTAINER_QUERY}"
                query_urls.append(url)
    return query_urls


def _fetch_container_listings(urls: list[str], dst: Path) -> None:
    """Fetch each container-listing URL and write all responses to *dst*."""
    with dst.open("w") as out:
        for i, url in enumerate(urls, 1):
            logger.step(i, len(urls), f"Fetching container: {url[:60]}…")
            try:
                resp = requests.get(url, timeout=AE_REQUEST_TIMEOUT)
                resp.raise_for_status()
                out.write(f"URL: {url}\nResponse Content:\n{resp.text}\n\n")
            except requests.HTTPError as exc:
                # 4xx/5xx – container may be private or not exist; not fatal
                out.write(f"HTTP error for {url}: {exc}\n")
            except requests.RequestException as exc:
                out.write(f"Error retrieving {url}: {exc}\n")
    logger.info("")  # flush the progress-bar line


def _extract_blob_urls(ae3: Path) -> list[str]:
    """Extract every <Url>…</Url> value from the raw container listings."""
    text = ae3.read_text()
    return [m.strip() for m in re.findall(AZURE_URL_TAG_PATTERN, text, re.DOTALL)]


def _count_extensions(urls: list[str], ext_file: Path) -> dict[str, int]:
    """Count file extensions that appear in the allowed-extensions list."""
    # Load allowed extensions
    allowed: set[str] = set()
    if ext_file.exists():
        for line in ext_file.read_text().splitlines():
            ext = line.strip()
            if ext:
                allowed.add(ext)
    else:
        logger.warn(f"Extensions file not found: {ext_file}  (counting all extensions)")

    counts: dict[str, int] = defaultdict(int)
    for url in urls:
        if url:
            ext = os.path.splitext(urlparse(url).path)[1]
            if not allowed or ext in allowed:   # if no whitelist, count all
                counts[ext] += 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


def _write_ext_counts(counts: dict[str, int], dst: Path) -> None:
    with dst.open("w") as fh:
        for ext, count in counts.items():
            fh.write(f"Extension: {ext}, Count: {count}\n")
