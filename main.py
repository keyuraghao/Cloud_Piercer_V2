"""
main.py  –  Cloud Pearser entry point.

Run with:
    python main.py
    python main.py --keywords my_keywords.txt
    python main.py --keywords my_keywords.txt --skip-azure-enum
    python main.py --use-cache          # re-parse previously fetched API data

Bug fixed from original Cloud_pearser.py:

  BUG 1 (original): The timestamp file was written inside the `with open(...)
                     as file:` block, and the stale-file cleanup loop was also
                     INSIDE that same with-block (indented under it).  The
                     cleanup code therefore only ran when the folder already
                     existed – AND the file writes also happened inside the
                     `else` branch of the directory-exists check, so on a
                     first run the time_stamp.txt was never created, causing
                     every subsequent read of it to crash with FileNotFoundError.
                     Fixed by separating directory creation, cleanup, and
                     timestamp writing into clearly ordered steps.

  BUG 2 (original): output_location path was built as
                     current_directory + "/Outputs"  (double-slash) because
                     current_directory already ended with "/".
                     e.g.  /home/user//Outputs/  → worked by luck on Linux,
                     breaks on some systems.  Fixed using pathlib throughout.

  BUG 3 (original): The summary count read from Unique<TS>.txt but the file
                     was written by Unique.py as a subprocess; a race condition
                     existed where the main process could read before the
                     subprocess finished.  Fixed: all processing is now
                     sequential in-process.

  BUG 4 (original): `multiprocessing` was imported but never used.

  BUG 5 (original): API start_values were strings ("0", "1000") – see api.py.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from cloud_pearser import __version__
from cloud_pearser.api import fetch_all_keywords, flatten_to_lines, load_cached
from cloud_pearser.config import (
    EXTENSIONS_FILE,
    KEYWORDS_FILE,
    PROVIDERS,
    TMP_DIR,
)
from cloud_pearser.parsers import build_provider_csvs, build_unique, enumerate_azure
from cloud_pearser.utils import logger


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cloud_pearser",
        description="Search GrayHatWarfare for exposed cloud storage buckets.",
    )
    p.add_argument(
        "--keywords", "-k",
        type=Path, default=KEYWORDS_FILE,
        help=f"Path to keywords file (default: {KEYWORDS_FILE})",
    )
    p.add_argument(
        "--output-dir", "-o",
        type=Path, default=None,
        help="Base output directory (default: ./Outputs/<timestamp>_Outputs/)",
    )
    p.add_argument(
        "--skip-azure-enum",
        action="store_true",
        help="Do not run Azure Blob container enumeration.",
    )
    p.add_argument(
        "--use-cache",
        action="store_true",
        help="Skip API fetch; re-parse the most recent cached output_data.json.",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_dirs(base_output_dir: Path | None) -> tuple[Path, str]:
    """Create tmp dir, output dir, return (output_dir, timestamp)."""
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    _clean_tmp()

    if base_output_dir is None:
        base_output_dir = Path("Outputs") / f"{timestamp}_Outputs"
    base_output_dir.mkdir(parents=True, exist_ok=True)

    return base_output_dir, timestamp


def _clean_tmp() -> None:
    """Remove stale intermediate files from a previous run."""
    stale = [
        "output_data_1.txt", "output_data.json", "output_data.txt",
        "display.txt", "display1.txt", "display2.txt",
        "AWS_output_data_1.txt",  "AWS_output_data_2.txt",
        "Azure_output_data_1.txt","Azure_output_data_2.txt",
        "GCP_output_data_1.txt",  "GCP_output_data_2.txt",
        "DOS_output_data_1.txt",  "DOS_output_data_2.txt",
        "AE_1.txt", "AE_2.txt", "AE_3.txt", "AE_4.txt",
    ]
    removed = 0
    for name in stale:
        p = TMP_DIR / name
        if p.exists():
            p.unlink()
            removed += 1
    if removed:
        logger.info(f"Cleaned {removed} stale temp file(s) from {TMP_DIR}")


def _load_keywords(path: Path) -> list[str]:
    if not path.exists():
        logger.error(f"Keywords file not found: {path}")
        sys.exit(1)
    kws = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    if not kws:
        logger.error(f"Keywords file is empty: {path}")
        sys.exit(1)
    return kws


def _count_domains(unique_file: Path) -> dict[str, int]:
    """Count how many lines in *unique_file* contain each provider domain."""
    text = unique_file.read_text()
    return {name: text.count(domain) for name, domain in PROVIDERS.items()}


def _print_summary(counts: dict[str, int], output_dir: Path) -> None:
    total = sum(counts.values())
    logger.section("Results Summary")
    col_w = max(len(k) for k in counts) + 2
    print(f"  {'Provider':<{col_w}}  {'Count':>8}")
    print(f"  {'─' * col_w}  {'─' * 8}")
    for provider, count in counts.items():
        print(f"  {provider:<{col_w}}  {count:>8,}")
    print(f"  {'─' * col_w}  {'─' * 8}")
    print(f"  {'TOTAL':<{col_w}}  {total:>8,}")
    print(f"\n  Output directory: {output_dir.resolve()}")


def _save_summary_json(
    counts: dict[str, int],
    keywords: list[str],
    timestamp: str,
    output_dir: Path,
) -> None:
    """Write a machine-readable summary for the dashboard to consume."""
    summary = {
        "timestamp": timestamp,
        "total_keywords": len(keywords),
        "counts": counts,
        "total": sum(counts.values()),
    }
    dst = output_dir / f"summary_{timestamp}.json"
    dst.write_text(json.dumps(summary, indent=2))
    logger.info(f"Summary JSON  →  {dst.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    logger.banner(f"Cloud Pearser  v{__version__}")

    # ── Setup ──────────────────────────────────────────────────────────────
    output_dir, timestamp = _setup_dirs(args.output_dir)
    logger.info(f"Output directory: {output_dir.resolve()}")
    logger.info(f"Timestamp: {timestamp}")

    # ── Keywords ───────────────────────────────────────────────────────────
    keywords = _load_keywords(args.keywords)
    logger.info(f"Keywords loaded: {len(keywords):,}  ({args.keywords})")

    # ── Copy support files to output dir ───────────────────────────────────
    for src in [args.keywords, EXTENSIONS_FILE]:
        if src.exists():
            shutil.copy2(src, output_dir / src.name)

    # ── API fetch (or load cache) ──────────────────────────────────────────
    flat_file = TMP_DIR / "output_data_1.txt"

    if args.use_cache:
        logger.info("--use-cache: loading cached API data …")
        raw = load_cached(TMP_DIR)
        if raw is None:
            logger.error("No cached data found. Run without --use-cache first.")
            sys.exit(1)
        flatten_to_lines(raw, flat_file)
    else:
        raw = fetch_all_keywords(keywords, TMP_DIR)
        flatten_to_lines(raw, flat_file)

    # ── Unique bucket list ─────────────────────────────────────────────────
    unique_file = output_dir / f"Unique_{timestamp}.txt"
    build_unique(flat_file, unique_file)

    # ── Per-provider CSVs ──────────────────────────────────────────────────
    build_provider_csvs(flat_file, output_dir, timestamp)

    # ── Azure Enumeration ──────────────────────────────────────────────────
    if not args.skip_azure_enum:
        azure_csv = output_dir / f"Azure_{timestamp}.csv"
        if azure_csv.exists() and azure_csv.stat().st_size > 0:
            enumerate_azure(
                azure_csv=azure_csv,
                ext_file=EXTENSIONS_FILE,
                output_dir=output_dir,
                timestamp=timestamp,
                tmp_dir=TMP_DIR,
            )
        else:
            logger.warn("Azure CSV is empty or missing – skipping enumeration.")
    else:
        logger.info("Azure enumeration skipped (--skip-azure-enum).")

    # ── Summary ───────────────────────────────────────────────────────────
    counts = _count_domains(unique_file)
    _print_summary(counts, output_dir)
    _save_summary_json(counts, keywords, timestamp, output_dir)

    logger.banner("Cloud Pearsing Finished")


if __name__ == "__main__":
    main()
