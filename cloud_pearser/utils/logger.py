"""
utils/logger.py  –  Lightweight coloured console output.
No external dependencies.
"""

from __future__ import annotations

import sys
from datetime import datetime

# ANSI colours (disabled automatically on non-tty outputs)
_USE_COLOUR = sys.stdout.isatty()

_RESET  = "\033[0m"  if _USE_COLOUR else ""
_BOLD   = "\033[1m"  if _USE_COLOUR else ""
_CYAN   = "\033[96m" if _USE_COLOUR else ""
_GREEN  = "\033[92m" if _USE_COLOUR else ""
_YELLOW = "\033[93m" if _USE_COLOUR else ""
_RED    = "\033[91m" if _USE_COLOUR else ""
_DIM    = "\033[2m"  if _USE_COLOUR else ""


def _ts() -> str:
    return f"{_DIM}{datetime.now().strftime('%H:%M:%S')}{_RESET}"


def banner(title: str) -> None:
    bar = "+" + "-" * (len(title) + 2) + "+"
    print(f"\n{_BOLD}{_CYAN}{bar}")
    print(f"| {title} |")
    print(f"{bar}{_RESET}\n")


def info(msg: str) -> None:
    print(f"{_ts()}  {_CYAN}[INFO]{_RESET}  {msg}")


def ok(msg: str) -> None:
    print(f"{_ts()}  {_GREEN}[ OK ]{_RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"{_ts()}  {_YELLOW}[WARN]{_RESET}  {msg}")


def error(msg: str) -> None:
    print(f"{_ts()}  {_RED}[ERR ]{_RESET}  {msg}", file=sys.stderr)


def step(n: int, total: int, msg: str) -> None:
    pct = int(n / total * 100) if total else 0
    bar_len = 20
    filled  = int(bar_len * n / total) if total else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r{_ts()}  [{bar}] {pct:3d}%  {msg}", end="", flush=True)
    if n == total:
        print()  # newline at 100 %


def section(title: str) -> None:
    print(f"\n{_BOLD}── {title} {'─' * max(0, 50 - len(title))}{_RESET}")
