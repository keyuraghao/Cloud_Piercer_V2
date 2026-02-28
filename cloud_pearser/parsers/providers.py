"""
parsers/providers.py  –  Filter raw API lines by cloud provider and write CSVs.

One function per provider (AWS, GCP, DigitalOcean each produce a 2-column CSV;
Azure produces a 3-column CSV).  All four share the same core logic.

Bug fixed from original AWS.py / Azure.py / GCP.py / DOS.py:

  BUG (original): remove_words_and_characters() referenced words_to_remove and
                  characters_to_remove as global names, but those variables were
                  defined AFTER the function definition.  In a standalone script
                  this works if both are defined at module level before the
                  function is *called*, but the layout was fragile and confusing.
                  Fixed by passing the lists explicitly.

  BUG (Azure):    The trailing-row append block in the original Azure.py ran
                  unconditionally AFTER the main loop, meaning it would ALWAYS
                  append the last row a second time (double-writing the final
                  row) unless the line count happened to be an exact multiple of
                  3.  Fixed in write_csv_3col() in utils/files.py.
"""

from __future__ import annotations

import csv as _csv
import re
from pathlib import Path
from urllib.parse import urlparse

from ..config import PROVIDERS, STRIP_CHARS, STRIP_WORDS
from ..utils.files import (
    clean_line,
    filter_lines_containing,
    write_csv_2col,
    write_csv_3col,
)
from ..utils import logger


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_and_clean(flat_file: Path, include_words: list[str]) -> list[str]:
    """Filter *flat_file* lines, then clean each matching line."""
    raw = filter_lines_containing(flat_file, include_words)
    return [clean_line(ln, STRIP_WORDS, STRIP_CHARS) for ln in raw]


# ---------------------------------------------------------------------------
# Per-provider builders
# ---------------------------------------------------------------------------

def build_aws(flat_file: Path, output_csv: Path) -> int:
    """Extract AWS S3 entries and write a 2-column CSV (Bucket, URL)."""
    logger.section("Parsing AWS")
    lines = _extract_and_clean(flat_file, [PROVIDERS["AWS"]])
    rows = write_csv_2col(lines, output_csv, headers=("Bucket", "URL"))
    logger.ok(f"AWS: {rows:,} rows  →  {output_csv.name}")
    return rows


def build_gcp(flat_file: Path, output_csv: Path) -> int:
    """Extract GCP Storage entries and write a 2-column CSV."""
    logger.section("Parsing GCP")
    lines = _extract_and_clean(flat_file, [PROVIDERS["GCP"]])
    rows = write_csv_2col(lines, output_csv, headers=("Bucket", "URL"))
    logger.ok(f"GCP: {rows:,} rows  →  {output_csv.name}")
    return rows


def build_digitalocean(flat_file: Path, output_csv: Path) -> int:
    """Extract DigitalOcean Spaces entries and write a 2-column CSV."""
    logger.section("Parsing DigitalOcean")
    lines = _extract_and_clean(flat_file, [PROVIDERS["DigitalOcean"]])
    rows = write_csv_2col(lines, output_csv, headers=("Bucket", "URL"))
    logger.ok(f"DigitalOcean: {rows:,} rows  →  {output_csv.name}")
    return rows


def build_azure(flat_file: Path, output_csv: Path) -> int:
    """
    Extract Azure Blob Storage entries and write a 3-column CSV
    (Storage Account, URL, Container).

    Each row is derived from a single URL: the storage account and container
    are parsed from the URL itself so the columns are semantically correct.
    This means _build_query_urls() in azure_enum.py receives valid data and
    can correctly build container-listing URLs like:
        https://<storage>/<container>?restype=container&comp=list
    """
    logger.section("Parsing Azure")

    azure_domain = PROVIDERS["Azure"]

    # All raw lines that mention the Azure blob domain
    domain_lines = filter_lines_containing(flat_file, [azure_domain])

    # Extract (storage_account, url, container) from each URL found on those lines.
    # Deduplicate by (storage, container) – one representative URL per container.
    seen_keys: set[tuple[str, str]] = set()
    rows: list[tuple[str, str, str]] = []

    for ln in domain_lines:
        m = re.search(r'https?://[^\s",]+', ln)
        if not m:
            continue
        raw_url = m.group(0).rstrip('",')
        try:
            parsed = urlparse(raw_url)
        except Exception:
            continue
        if azure_domain not in parsed.netloc:
            continue
        storage = parsed.netloc
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            continue
        container = path_parts[0]
        key = (storage, container)
        if key not in seen_keys:
            seen_keys.add(key)
            rows.append((storage, raw_url, container))

    rows_written = 0
    with output_csv.open("w", newline="") as fh:
        writer = _csv.writer(fh)
        writer.writerow(["Storage Account", "URL", "Container"])
        for row in rows:
            writer.writerow(list(row))
            rows_written += 1

    logger.ok(f"Azure: {rows_written:,} rows  →  {output_csv.name}")
    return rows_written


# ---------------------------------------------------------------------------
# Convenience: run all providers
# ---------------------------------------------------------------------------

def build_all(flat_file: Path, output_dir: Path, timestamp: str) -> dict[str, int]:
    """Build CSVs for every provider.  Returns {provider: row_count}."""
    results: dict[str, int] = {}
    results["AWS"]          = build_aws(flat_file,          output_dir / f"AWS_{timestamp}.csv")
    results["GCP"]          = build_gcp(flat_file,          output_dir / f"GCP_{timestamp}.csv")
    results["DigitalOcean"] = build_digitalocean(flat_file, output_dir / f"DigitalOcean_{timestamp}.csv")
    results["Azure"]        = build_azure(flat_file,        output_dir / f"Azure_{timestamp}.csv")
    return results
