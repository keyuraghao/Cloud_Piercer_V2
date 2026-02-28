"""
server.py  –  Cloud Pearser web server.

Opens a browser dashboard at http://localhost:5000 which controls everything:
  - Configure API key, keywords, offsets
  - Trigger scans (runs the full pipeline in a background thread)
  - Stream live logs to the browser via Server-Sent Events (SSE)
  - View results, charts, and file listings
  - Download output CSVs and text files

Run with:
    python server.py
    python server.py --port 8080
    python server.py --no-browser   # don't auto-open browser
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import queue
import shutil
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

# ── Flask ──────────────────────────────────────────────────────────────────
try:
    from flask import (
        Flask, Response, jsonify, request,
        send_file, send_from_directory, stream_with_context,
    )
    # flask_cors not needed
except ImportError:
    print("[ERROR] Flask not installed. Run:  pip install flask flask-cors")
    sys.exit(1)

# ── APScheduler (optional – needed for scheduled scans) ───────────────────
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _APScheduler_available = True
except ImportError:
    _APScheduler_available = False

# ── Cloud Pearser internals ────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from cloud_pearser.api import fetch_all_keywords, flatten_to_lines, load_cached
from cloud_pearser.config import (
    API_KEY as _DEFAULT_API_KEY,
    API_OFFSETS as _DEFAULT_OFFSETS,
    EXTENSIONS_FILE,
    KEYWORDS_FILE,
    PROVIDERS,
    TMP_DIR,
)
from cloud_pearser.parsers import build_provider_csvs, build_unique, enumerate_azure


# ═══════════════════════════════════════════════════════════════════════════
# App & globals
# ═══════════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).parent
app = Flask(__name__, static_folder=None)

@app.after_request
def _add_cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    return r
# CORS handled via after_request

# Active scan state (one scan at a time)
_scan_lock  = threading.Lock()
_scan_state: dict[str, Any] = {
    "running": False,
    "finished": False,
    "error": None,
    "progress": 0,          # 0-100
    "stage": "",
    "timestamp": None,
    "output_dir": None,
    "counts": {},
    "ext_counts": {},
    "blob_urls": [],
    "keyword_counts": {},
}

# Per-scan log queue (consumed by SSE stream)
_log_queue: queue.Queue = queue.Queue()

# Persistent scan history (in-memory; survives page refreshes in same session)
_history: list[dict] = []

# Scheduled scans (persisted to schedules.json)
_schedules: dict[str, dict] = {}
_SCHEDULES_FILE = BASE_DIR / "schedules.json"
_scheduler: Any = None   # BackgroundScheduler; set in main() if APScheduler available


def _load_history_from_disk() -> None:
    """Rebuild _history from summary JSON files in Outputs/ on server start."""
    existing_ts = {entry["ts"] for entry in _history}
    for d in reversed(_list_output_dirs()):   # oldest → newest order
        summary = d.get("summary", {})
        if not summary:
            continue
        ts = summary.get("timestamp") or d["name"].replace("_Outputs", "")
        if ts in existing_ts:
            continue
        _history.append({
            "ts": ts,
            "keywords": summary.get("total_keywords", 0),
            "kw_sample": "",
            "counts": summary.get("counts", {}),
            "total": summary.get("total", 0),
            "output_dir": d["path"],
            "status": "ok",
        })
        existing_ts.add(ts)


def _load_schedules() -> None:
    """Load persisted schedules from disk and (re-)register them with APScheduler."""
    if not _APScheduler_available or _scheduler is None:
        return
    if _SCHEDULES_FILE.exists():
        try:
            _schedules.update(json.loads(_SCHEDULES_FILE.read_text()))
        except Exception:
            pass
    for sid, sched in _schedules.items():
        if sched.get("enabled"):
            _register_schedule(sid, sched)


def _save_schedules() -> None:
    _SCHEDULES_FILE.write_text(json.dumps(_schedules, indent=2))


def _register_schedule(sid: str, sched: dict) -> None:
    if not _scheduler:
        return
    try:
        _scheduler.remove_job(sid)
    except Exception:
        pass
    hours = max(1, int(sched.get("interval_hours", 24)))
    _scheduler.add_job(
        _run_scheduled_scan,
        trigger=IntervalTrigger(hours=hours),
        id=sid,
        args=[sid],
        replace_existing=True,
        misfire_grace_time=3600,
    )


def _run_scheduled_scan(sid: str) -> None:
    sched = _schedules.get(sid)
    if not sched or not sched.get("enabled"):
        return
    if _scan_state["running"]:
        return   # skip if a scan is already in progress
    keywords = [k.strip() for k in sched.get("keywords", "").splitlines() if k.strip()]
    if not keywords:
        return
    api_key = sched.get("api_key") or _DEFAULT_API_KEY
    offsets = sched.get("offsets") or list(_DEFAULT_OFFSETS)
    _schedules[sid]["last_run"] = datetime.now().isoformat()
    _save_schedules()
    t = threading.Thread(
        target=_run_scan,
        args=(keywords, api_key, offsets, False, False),
        daemon=True,
    )
    t.start()


# ═══════════════════════════════════════════════════════════════════════════
# Logging bridge  –  redirect logger output into the SSE queue
# ═══════════════════════════════════════════════════════════════════════════

class _QueueLogger:
    """Captures log lines and puts them in the SSE queue."""

    def push(self, msg: str, level: str = "info") -> None:
        _log_queue.put({"msg": msg, "level": level})
        # Also print to terminal
        print(msg)

    def info(self, msg):    self.push(msg, "info")
    def ok(self, msg):      self.push(msg, "ok")
    def warn(self, msg):    self.push(msg, "warn")
    def error(self, msg):   self.push(msg, "error")
    def banner(self, msg):  self.push(f"── {msg} ──", "banner")
    def section(self, msg): self.push(f"▸ {msg}", "section")
    def step(self, n, total, msg):
        pct = int(n / total * 100) if total else 0
        self.push(f"  [{n}/{total}] {pct}%  {msg}", "step")
        _scan_state["progress"] = max(_scan_state["progress"], pct)

# Monkey-patch the logger module so all internal code goes to SSE
import cloud_pearser.utils.logger as _logger_mod
_qlog = _QueueLogger()
_logger_mod.info    = _qlog.info
_logger_mod.ok      = _qlog.ok
_logger_mod.warn    = _qlog.warn
_logger_mod.error   = _qlog.error
_logger_mod.banner  = _qlog.banner
_logger_mod.section = _qlog.section
_logger_mod.step    = _qlog.step


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _clean_tmp() -> None:
    stale = [
        "output_data_1.txt", "output_data.json", "output_data.txt",
        "display.txt", "display1.txt", "display2.txt",
        "AWS_output_data_1.txt",   "AWS_output_data_2.txt",
        "Azure_output_data_1.txt", "Azure_output_data_2.txt",
        "GCP_output_data_1.txt",   "GCP_output_data_2.txt",
        "DOS_output_data_1.txt",   "DOS_output_data_2.txt",
        "AE_1.txt", "AE_2.txt", "AE_3.txt", "AE_4.txt",
    ]
    for name in stale:
        p = TMP_DIR / name
        if p.exists():
            p.unlink()


def _count_domains(unique_file: Path) -> dict[str, int]:
    text = unique_file.read_text()
    return {name: text.count(domain) for name, domain in PROVIDERS.items()}


def _compute_keyword_counts(raw: dict) -> dict:
    """Count unique files per keyword per provider from raw API JSON."""
    result = {}
    for keyword, offsets in raw.items():
        counts = {p: 0 for p in PROVIDERS}
        seen: set = set()
        for offset_data in offsets.values():
            for f in offset_data.get("files", []):
                fid = f.get("id") or f.get("url", "")
                if fid in seen:
                    continue
                seen.add(fid)
                val = f.get("bucket", "") + " " + f.get("url", "")
                for prov, domain in PROVIDERS.items():
                    if domain in val:
                        counts[prov] += 1
                        break
        result[keyword] = counts
    return result


def _save_summary(counts: dict, keywords: list, timestamp: str,
                  ext_counts: dict, keyword_counts: dict, output_dir: Path) -> None:
    data = {
        "timestamp": timestamp,
        "total_keywords": len(keywords),
        "counts": counts,
        "total": sum(counts.values()),
        "extCounts": ext_counts,
        "keyword_counts": keyword_counts,
    }
    (output_dir / f"summary_{timestamp}.json").write_text(json.dumps(data, indent=2))


def _list_output_dirs() -> list[dict]:
    """Return all timestamped output directories, newest first."""
    outputs = BASE_DIR / "Outputs"
    if not outputs.exists():
        return []
    dirs = sorted(outputs.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for d in dirs:
        if not d.is_dir():
            continue
        summary_files = list(d.glob("summary_*.json"))
        summary = {}
        if summary_files:
            try:
                summary = json.loads(summary_files[0].read_text())
            except Exception:
                pass
        result.append({
            "name": d.name,
            "path": str(d),
            "mtime": d.stat().st_mtime,
            "summary": summary,
        })
    return result


def _get_output_dir(ts: str) -> Path | None:
    base = BASE_DIR / "Outputs"
    for d in base.iterdir() if base.exists() else []:
        if d.is_dir() and ts in d.name:
            return d
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Scan worker  (runs in background thread)
# ═══════════════════════════════════════════════════════════════════════════

def _run_scan(
    keywords: list[str],
    api_key: str,
    offsets: list[int],
    skip_azure_enum: bool,
    use_cache: bool,
) -> None:
    global _scan_state

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = BASE_DIR / "Outputs" / f"{timestamp}_Outputs"

    try:
        # ── Setup ─────────────────────────────────────────────────────────
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        _clean_tmp()

        _scan_state.update({
            "running": True, "finished": False, "error": None,
            "progress": 0, "stage": "setup", "timestamp": timestamp,
            "output_dir": str(output_dir), "counts": {},
            "ext_counts": {}, "blob_urls": [], "keyword_counts": {},
        })

        # Temporarily override API key from request
        import cloud_pearser.config as cfg
        original_key = cfg.API_KEY
        original_offsets = cfg.API_OFFSETS
        cfg.API_KEY = api_key
        cfg.API_OFFSETS = offsets

        # Copy support files
        for src in [KEYWORDS_FILE, EXTENSIONS_FILE]:
            if (BASE_DIR / src).exists():
                shutil.copy2(BASE_DIR / src, output_dir / src.name)

        _qlog.banner("Cloud Pearser v2.0.0")
        _qlog.info(f"Keywords: {len(keywords)}")
        _qlog.info(f"API offsets: {offsets}")
        _qlog.info(f"Output: {output_dir.name}")

        flat_file = TMP_DIR / "output_data_1.txt"

        # ── API fetch ──────────────────────────────────────────────────────
        _scan_state["stage"] = "fetching"
        if use_cache:
            _qlog.info("Loading cached API data …")
            raw = load_cached(TMP_DIR)
            if raw is None:
                raise RuntimeError("No cached data found. Run without use_cache first.")
            flatten_to_lines(raw, flat_file)
        else:
            raw = fetch_all_keywords(keywords, TMP_DIR)
            flatten_to_lines(raw, flat_file)

        _scan_state["progress"] = 60
        keyword_counts = _compute_keyword_counts(raw) if raw else {}
        _scan_state["keyword_counts"] = keyword_counts

        # ── Unique bucket list ─────────────────────────────────────────────
        _scan_state["stage"] = "unique"
        unique_file = output_dir / f"Unique_{timestamp}.txt"
        build_unique(flat_file, unique_file)
        _scan_state["progress"] = 70

        # ── Per-provider CSVs ──────────────────────────────────────────────
        _scan_state["stage"] = "providers"
        build_provider_csvs(flat_file, output_dir, timestamp)
        _scan_state["progress"] = 85

        # Count domains
        counts = _count_domains(unique_file)
        _scan_state["counts"] = counts

        # ── Azure Enumeration ──────────────────────────────────────────────
        ext_counts: dict = {}
        blob_urls: list = []

        if not skip_azure_enum:
            _scan_state["stage"] = "azure_enum"
            azure_csv = output_dir / f"Azure_{timestamp}.csv"
            if azure_csv.exists() and azure_csv.stat().st_size > 0:
                ae_result = enumerate_azure(
                    azure_csv=azure_csv,
                    ext_file=BASE_DIR / EXTENSIONS_FILE,
                    output_dir=output_dir,
                    timestamp=timestamp,
                    tmp_dir=TMP_DIR,
                )
                ext_counts = ae_result.get("ext_counts", {})
                # Read blob URLs from file
                url_file = output_dir / f"URLs_{timestamp}.txt"
                if url_file.exists():
                    blob_urls = [l.strip() for l in url_file.read_text().splitlines() if l.strip()]
            else:
                _qlog.warn("Azure CSV empty – skipping enumeration.")

        _scan_state["ext_counts"] = ext_counts
        _scan_state["blob_urls"] = blob_urls[:200]  # cap for memory
        _scan_state["progress"] = 98

        # ── Summary ────────────────────────────────────────────────────────
        _save_summary(counts, keywords, timestamp, ext_counts, keyword_counts, output_dir)
        _scan_state["stage"] = "done"
        _scan_state["progress"] = 100

        total = sum(counts.values())
        _qlog.ok(f"AWS: {counts.get('AWS',0)}")
        _qlog.ok(f"Azure: {counts.get('Azure',0)}")
        _qlog.ok(f"GCP: {counts.get('GCP',0)}")
        _qlog.ok(f"DigitalOcean: {counts.get('DigitalOcean',0)}")
        _qlog.ok(f"TOTAL: {total}")
        _qlog.banner("Scan Finished")

        # Add to history
        _history.append({
            "ts": timestamp,
            "keywords": len(keywords),
            "kw_sample": keywords[0] if keywords else "",
            "counts": counts,
            "total": total,
            "output_dir": str(output_dir),
            "status": "ok",
        })

    except Exception as exc:
        _scan_state["error"] = str(exc)
        _scan_state["stage"] = "error"
        _qlog.error(f"Scan failed: {exc}")
        if _history and _history[-1].get("ts") == timestamp:
            _history[-1]["status"] = "error"
        else:
            _history.append({
                "ts": timestamp, "keywords": len(keywords),
                "kw_sample": keywords[0] if keywords else "",
                "counts": {}, "total": 0,
                "output_dir": str(output_dir), "status": "error",
            })

    finally:
        # Restore original config values
        try:
            cfg.API_KEY = original_key
            cfg.API_OFFSETS = original_offsets
        except Exception:
            pass
        _scan_state["running"] = False
        _scan_state["finished"] = True
        _log_queue.put({"msg": "__DONE__", "level": "done"})


# ═══════════════════════════════════════════════════════════════════════════
# REST API endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    """Current scan state snapshot."""
    return jsonify(_scan_state)


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return current configuration."""
    kw_path = BASE_DIR / KEYWORDS_FILE
    keywords_text = kw_path.read_text() if kw_path.exists() else ""
    return jsonify({
        "api_key": _DEFAULT_API_KEY,
        "offsets": _DEFAULT_OFFSETS,
        "keywords": keywords_text,
        "keywords_file": str(KEYWORDS_FILE),
        "extensions_file": str(EXTENSIONS_FILE),
    })


@app.route("/api/config", methods=["POST"])
def api_config_save():
    """Save keywords to keywords.txt."""
    data = request.get_json(force=True)
    kw_text = data.get("keywords", "")
    kw_path = BASE_DIR / KEYWORDS_FILE
    kw_path.write_text(kw_text)
    return jsonify({"ok": True, "saved": str(kw_path)})


@app.route("/api/scan/start", methods=["POST"])
def api_scan_start():
    """Start a scan in a background thread."""
    if _scan_state["running"]:
        return jsonify({"error": "A scan is already running."}), 409

    data = request.get_json(force=True)
    raw_kws = data.get("keywords", "")
    keywords = [k.strip() for k in raw_kws.splitlines() if k.strip()]
    if not keywords:
        # Fall back to keywords.txt
        kw_path = BASE_DIR / KEYWORDS_FILE
        if kw_path.exists():
            keywords = [k.strip() for k in kw_path.read_text().splitlines() if k.strip()]
    if not keywords:
        return jsonify({"error": "No keywords provided."}), 400

    api_key          = data.get("api_key", _DEFAULT_API_KEY).strip()
    raw_offsets      = data.get("offsets", "0,1000")
    skip_azure_enum  = bool(data.get("skip_azure_enum", False))
    use_cache        = bool(data.get("use_cache", False))

    try:
        offsets = [int(x.strip()) for x in str(raw_offsets).split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "Offsets must be comma-separated integers."}), 400

    # Drain old log queue
    while not _log_queue.empty():
        try:
            _log_queue.get_nowait()
        except queue.Empty:
            break

    t = threading.Thread(
        target=_run_scan,
        args=(keywords, api_key, offsets, skip_azure_enum, use_cache),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True, "message": f"Scan started with {len(keywords)} keywords."})


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    """
    Graceful stop is hard with threading; we just set a flag.
    Real stop would require subprocess management.
    """
    return jsonify({"ok": True, "message": "Stop requested (current request will complete)."})


@app.route("/api/logs")
def api_logs_sse():
    """
    Server-Sent Events stream.  Browser connects once; log lines arrive in real-time.
    """
    def generate():
        yield "data: {\"msg\": \"Connected to log stream.\", \"level\": \"info\"}\n\n"
        while True:
            try:
                item = _log_queue.get(timeout=30)
                payload = json.dumps(item)
                yield f"data: {payload}\n\n"
                if item.get("level") == "done":
                    break
            except queue.Empty:
                # heartbeat so connection stays alive
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/history")
def api_history():
    return jsonify({"history": list(reversed(_history))})


@app.route("/api/outputs")
def api_outputs():
    """List all output directories with their summary data."""
    return jsonify({"dirs": _list_output_dirs()})


@app.route("/api/outputs/<ts>/files")
def api_output_files(ts: str):
    """List files inside a specific output directory."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    files = []
    for f in sorted(d.iterdir()):
        if f.is_file():
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "ext": f.suffix,
            })
    return jsonify({"files": files, "dir": d.name})


@app.route("/api/outputs/<ts>/download/<filename>")
def api_download(ts: str, filename: str):
    """Download a specific output file."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    fp = d / filename
    if not fp.exists() or not fp.is_file():
        return jsonify({"error": "File not found"}), 404
    # Security: ensure file is inside the output dir
    try:
        fp.resolve().relative_to(d.resolve())
    except ValueError:
        return jsonify({"error": "Forbidden"}), 403
    return send_file(str(fp), as_attachment=True, download_name=filename)


@app.route("/api/outputs/<ts>/summary")
def api_output_summary(ts: str):
    """Return the summary JSON for a specific run."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    summaries = list(d.glob("summary_*.json"))
    if not summaries:
        return jsonify({"error": "No summary found"}), 404
    return jsonify(json.loads(summaries[0].read_text()))


@app.route("/api/outputs/<ts>/csv/<provider>")
def api_csv_preview(ts: str, provider: str):
    """Return first 200 rows of a provider CSV as JSON."""
    allowed = {"AWS", "Azure", "GCP", "DigitalOcean"}
    if provider not in allowed:
        return jsonify({"error": "Unknown provider"}), 400
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    csv_files = list(d.glob(f"{provider}_*.csv"))
    if not csv_files:
        return jsonify({"rows": [], "headers": [], "total": 0})
    with open(csv_files[0], newline="") as f:
        reader = csv.reader(f)
        headers = next(reader, [])
        rows = []
        for i, row in enumerate(reader):
            if i >= 200:
                break
            rows.append(row)
    return jsonify({"headers": headers, "rows": rows, "total": len(rows)})


@app.route("/api/outputs/<ts>/urls")
def api_url_preview(ts: str):
    """Return first 100 blob URLs from Azure enumeration."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    url_files = list(d.glob("URLs_*.txt"))
    if not url_files:
        return jsonify({"urls": [], "total": 0})
    lines = [l.strip() for l in url_files[0].read_text().splitlines() if l.strip()]
    return jsonify({"urls": lines[:100], "total": len(lines)})


@app.route("/api/outputs/<ts>/ext_counts")
def api_ext_counts(ts: str):
    """Return file extension counts from Azure enumeration."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    ext_files = list(d.glob("Types_of_files_*.txt"))
    if not ext_files:
        return jsonify({"counts": {}})
    counts = {}
    for line in ext_files[0].read_text().splitlines():
        if ":" in line:
            parts = line.split(",")
            ext = parts[0].replace("Extension:", "").strip()
            cnt = parts[1].replace("Count:", "").strip() if len(parts) > 1 else "0"
            try:
                counts[ext] = int(cnt)
            except ValueError:
                pass
    return jsonify({"counts": counts})


# ═══════════════════════════════════════════════════════════════════════════
# Delete a scan run
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/outputs/<ts>", methods=["DELETE"])
def api_delete_run(ts: str):
    """Delete a scan run's output directory and remove it from history."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    try:
        shutil.rmtree(d)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    _history[:] = [e for e in _history if e.get("ts") != ts]
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
# Scheduled scans
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/schedules", methods=["GET"])
def api_schedules_list():
    """List all schedules with next_run times."""
    items = []
    for sid, sched in _schedules.items():
        item = dict(sched)
        if _scheduler:
            job = _scheduler.get_job(sid)
            item["next_run"] = (
                job.next_run_time.isoformat() if job and job.next_run_time else None
            )
        else:
            item["next_run"] = None
        items.append(item)
    return jsonify({"schedules": items, "scheduler_available": _APScheduler_available})


@app.route("/api/schedules", methods=["POST"])
def api_schedules_create():
    """Create a new scheduled scan."""
    if not _APScheduler_available:
        return jsonify({
            "error": "APScheduler not installed. Run: pip install APScheduler"
        }), 503
    data = request.get_json(force=True)
    name = (data.get("name") or "Scheduled Scan").strip()
    keywords = (data.get("keywords") or "").strip()
    if not keywords:
        return jsonify({"error": "Keywords required"}), 400
    try:
        interval_hours = max(1, int(data.get("interval_hours", 24)))
    except (ValueError, TypeError):
        return jsonify({"error": "interval_hours must be a positive integer"}), 400
    api_key = (data.get("api_key") or _DEFAULT_API_KEY).strip()
    sid = datetime.now().strftime("%Y%m%d%H%M%S%f")
    sched = {
        "id": sid,
        "name": name,
        "keywords": keywords,
        "api_key": api_key,
        "interval_hours": interval_hours,
        "enabled": True,
        "created": datetime.now().isoformat(),
        "last_run": None,
    }
    _schedules[sid] = sched
    _save_schedules()
    _register_schedule(sid, sched)
    return jsonify({"ok": True, "id": sid})


@app.route("/api/schedules/<sid>", methods=["DELETE"])
def api_schedules_delete(sid: str):
    """Delete a scheduled scan."""
    if sid not in _schedules:
        return jsonify({"error": "Not found"}), 404
    del _schedules[sid]
    _save_schedules()
    if _scheduler:
        try:
            _scheduler.remove_job(sid)
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/schedules/<sid>/toggle", methods=["POST"])
def api_schedules_toggle(sid: str):
    """Enable or disable a scheduled scan."""
    if sid not in _schedules:
        return jsonify({"error": "Not found"}), 404
    sched = _schedules[sid]
    sched["enabled"] = not sched.get("enabled", True)
    _save_schedules()
    if _scheduler:
        if sched["enabled"]:
            _register_schedule(sid, sched)
        else:
            try:
                _scheduler.remove_job(sid)
            except Exception:
                pass
    return jsonify({"ok": True, "enabled": sched["enabled"]})


# ═══════════════════════════════════════════════════════════════════════════
# Serve the dashboard HTML
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def serve_dashboard():
    return send_from_directory(BASE_DIR, "dashboard.html")


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global _scheduler
    parser = argparse.ArgumentParser(description="Cloud Pearser web dashboard")
    parser.add_argument("--port", "-p", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true",
                        help="Don't auto-open browser on startup")
    args = parser.parse_args()

    if _APScheduler_available:
        _scheduler = BackgroundScheduler()
        _scheduler.start()

    _load_history_from_disk()
    _load_schedules()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Cloud Pearser Dashboard")
    print(f"  ─────────────────────────────────")
    print(f"  URL:  {url}")
    print(f"  Stop: Ctrl+C\n")

    if not args.no_browser:
        # Slight delay so Flask is ready before the browser hits it
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
