"""
parsers/unique.py  –  Extract and deduplicate all bucket names / URLs.

Logic ported from Unique.py with the following bug-fixes:

  BUG 1 (original):  remove_words_and_characters() was called BEFORE
                      words_to_remove / characters_to_remove were defined.
                      Python evaluates module-level code top-to-bottom, so
                      this raised a NameError at runtime on the very first
                      invocation.  Fixed by defining the helper as a proper
                      function that takes its lists as arguments.

  BUG 2 (original):  The "include bucket / exclude buckets+bucketId+notice"
                      two-pass filter could be collapsed into a single pass
                      with a more precise predicate, avoiding writing two
                      intermediate temp files.
"""

from __future__ import annotations

from pathlib import Path

from ..config import STRIP_CHARS, STRIP_WORDS, UNIQUE_EXCLUDE_WORDS
from ..utils.files import (
    clean_line,
    deduplicate_lines,
    filter_lines_containing,
    filter_lines_excluding,
    write_lines,
)
from ..utils import logger


def build_unique(flat_file: Path, output_file: Path) -> list[str]:
    """
    1. Keep lines that contain "bucket" (the raw JSON key that holds bucket data).
    2. Drop lines that contain any of UNIQUE_EXCLUDE_WORDS
       ("buckets", "bucketId", "notice") because those are JSON structural keys,
       not actual bucket names.
    3. Strip the word "bucket" and punctuation from each remaining line.
    4. Deduplicate and write to *output_file*.

    Returns the final unique line list.
    """
    logger.section("Building unique bucket list")

    # Step 1 – keep lines with "bucket"
    raw = filter_lines_containing(flat_file, ["bucket"])
    logger.info(f"Lines containing 'bucket': {len(raw):,}")

    # Step 2 – drop structural JSON keys
    filtered = filter_lines_excluding(raw, UNIQUE_EXCLUDE_WORDS)
    logger.info(f"After excluding structural keys: {len(filtered):,}")

    # Step 3 – clean each line
    cleaned = [clean_line(ln, STRIP_WORDS, STRIP_CHARS) for ln in filtered]

    # Step 4 – deduplicate
    unique = deduplicate_lines(cleaned)
    write_lines(output_file, unique)

    logger.ok(f"Unique buckets: {len(unique):,}  →  {output_file.name}")
    return unique
