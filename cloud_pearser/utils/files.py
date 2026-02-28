"""
utils/files.py  –  Shared file-manipulation helpers.

These are pure functions; they never import from the rest of the project so
they can be used safely by any module without circular imports.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

def clean_line(line: str, strip_words: Iterable[str], strip_chars: Iterable[str]) -> str:
    """Remove every word in *strip_words* and every char in *strip_chars* from *line*."""
    for word in strip_words:
        line = line.replace(word, "")
    for ch in strip_chars:
        line = line.replace(ch, "")
    return line.strip()


# ---------------------------------------------------------------------------
# Line filtering
# ---------------------------------------------------------------------------

def filter_lines_containing(src: Path, include_words: Iterable[str]) -> list[str]:
    """Return lines from *src* that contain **any** word in *include_words*."""
    include_words = list(include_words)
    results: list[str] = []
    with src.open() as fh:
        for line in fh:
            if any(w in line for w in include_words):
                results.append(line)
    return results


def filter_lines_excluding(lines: Iterable[str], exclude_words: Iterable[str]) -> list[str]:
    """Drop lines that contain **any** word in *exclude_words*."""
    exclude_words = set(exclude_words)
    return [ln for ln in lines if not any(w in ln for w in exclude_words)]


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate_lines(lines: Iterable[str]) -> list[str]:
    """Return unique non-empty stripped lines, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for ln in lines:
        cleaned = ln.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def deduplicate_file(src: Path, dst: Path) -> int:
    """Write deduplicated lines from *src* to *dst*.  Returns number of unique lines."""
    unique = deduplicate_lines(src.read_text().splitlines())
    dst.write_text("\n".join(unique) + ("\n" if unique else ""))
    return len(unique)


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def write_csv_2col(lines: list[str], dst: Path, headers: tuple[str, str] = ("Bucket", "URL")) -> int:
    """
    Write *lines* into a 2-column CSV.
    Consecutive pairs → (col1, col2).  An odd trailing line gets col2="".
    Returns number of data rows written.
    """
    rows_written = 0
    with dst.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(headers))
        for i in range(0, len(lines), 2):
            col1 = lines[i]
            col2 = lines[i + 1] if i + 1 < len(lines) else ""
            writer.writerow([col1, col2])
            rows_written += 1
    return rows_written


def write_csv_3col(
    lines: list[str],
    dst: Path,
    headers: tuple[str, str, str] = ("Storage Account", "URL", "Container"),
) -> int:
    """
    Write *lines* into a 3-column CSV.
    Consecutive triples → (col1, col2, col3).  Short trailing rows are padded.
    Returns number of data rows written.
    """
    rows_written = 0
    with dst.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(list(headers))
        for i in range(0, len(lines), 3):
            row = lines[i : i + 3]
            while len(row) < 3:
                row.append("")
            writer.writerow(row)
            rows_written += 1
    return rows_written


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def write_lines(path: Path, lines: Iterable[str]) -> None:
    """Write an iterable of strings to *path*, one per line."""
    with path.open("w") as fh:
        for ln in lines:
            fh.write(ln.rstrip("\n") + "\n")
