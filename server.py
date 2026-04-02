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
import json
import queue
import shutil
import sys
import multiprocessing
import threading
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
    print("[ERROR] Flask not installed. Run:  pip install flask")
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
from cloud_pearser.config import (
    API_KEY as _DEFAULT_API_KEY,
    API_OFFSETS as _DEFAULT_OFFSETS,
    EXTENSIONS_FILE,
    KEYWORDS_FILE,
)
# parsers are imported locally inside _run_scan_process (runs in child process)


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

# Active scans – keyed by scan_id (= timestamp string).
# Each entry: {"id", "state", "sse_queue", "process"}
_active_scans: dict[str, dict] = {}

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
    if sched.get("interval_minutes"):
        minutes = max(1, int(sched["interval_minutes"]))
        trigger = IntervalTrigger(minutes=minutes)
    else:
        hours = max(1, int(sched.get("interval_hours", 24)))
        trigger = IntervalTrigger(hours=hours)
    _scheduler.add_job(
        _run_scheduled_scan,
        trigger=trigger,
        id=sid,
        args=[sid],
        replace_existing=True,
        misfire_grace_time=3600,
    )


def _run_scheduled_scan(sid: str) -> None:
    sched = _schedules.get(sid)
    if not sched or not sched.get("enabled"):
        return
    keywords = [k.strip() for k in sched.get("keywords", "").splitlines() if k.strip()]
    if not keywords:
        return
    api_key = sched.get("api_key") or _DEFAULT_API_KEY
    offsets = sched.get("offsets") or list(_DEFAULT_OFFSETS)
    _schedules[sid]["last_run"] = datetime.now().isoformat()
    _save_schedules()
    _start_scan_internal(keywords, api_key, offsets)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


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
# Scan pipeline  –  child process + bridge thread
# ═══════════════════════════════════════════════════════════════════════════

def _run_scan_process(
    keywords: list,
    api_key: str,
    offsets: list,
    skip_azure_enum: bool,
    use_cache: bool,
    comm_q,            # multiprocessing.Queue
    timestamp: str,
    output_dir_str: str,
    base_dir_str: str,
) -> None:
    """
    Full scan pipeline that runs inside a child process.
    All communication back to the parent goes through comm_q:
      {type:"log",      msg:str, level:str}
      {type:"progress", value:int, stage:str}
      {type:"done",     counts, ext_counts, blob_urls, keyword_counts, timestamp, output_dir, keywords}
      {type:"error",    error:str}
    """
    import sys as _sys, json as _json, shutil as _sh
    from pathlib import Path as _P

    base_dir   = _P(base_dir_str)
    output_dir = _P(output_dir_str)

    # ── Per-process logger writes to comm_q ────────────────────────────────
    class _PL:
        def _q(self, msg, level):
            try: comm_q.put_nowait({"type": "log", "msg": str(msg), "level": level})
            except Exception: pass
        def info(self, m):    self._q(m, "info")
        def ok(self, m):      self._q(m, "ok")
        def warn(self, m):    self._q(m, "warn")
        def error(self, m):   self._q(m, "error")
        def banner(self, m):  self._q(f"── {m} ──", "banner")
        def section(self, m): self._q(f"▸ {m}", "section")
        def step(self, n, total, msg):
            pct = int(n / total * 100) if total else 0
            self._q(f"  [{n}/{total}] {pct}%  {msg}", "step")
            try: comm_q.put_nowait({"type": "progress", "value": pct})
            except Exception: pass

    _sys.path.insert(0, base_dir_str)
    import cloud_pearser.utils.logger as _lmod
    _pl = _PL()
    _lmod.info = _pl.info; _lmod.ok = _pl.ok; _lmod.warn = _pl.warn
    _lmod.error = _pl.error; _lmod.banner = _pl.banner
    _lmod.section = _pl.section; _lmod.step = _pl.step

    def _prog(v, stage=""):
        try: comm_q.put_nowait({"type": "progress", "value": v, "stage": stage})
        except Exception: pass

    from cloud_pearser.api import fetch_all_keywords, flatten_to_lines, load_cached
    import cloud_pearser.config as _cfg
    from cloud_pearser.config import EXTENSIONS_FILE, KEYWORDS_FILE, PROVIDERS, TMP_DIR
    from cloud_pearser.parsers import build_provider_csvs, build_unique, enumerate_azure

    orig_key, orig_offsets = _cfg.API_KEY, _cfg.API_OFFSETS
    # Per-scan tmp dir – avoids file conflicts between concurrent scans
    scan_tmp = output_dir / "_tmp"

    try:
        scan_tmp.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        _cfg.API_KEY = api_key
        _cfg.API_OFFSETS = offsets

        for src in [KEYWORDS_FILE, EXTENSIONS_FILE]:
            p = base_dir / src
            if p.exists():
                _sh.copy2(p, output_dir / _P(src).name)

        _pl.banner("Cloud Pearser v2.0.0")
        _pl.info(f"Keywords : {len(keywords)}")
        _pl.info(f"Offsets  : {offsets}")
        _prog(5, "setup")

        flat_file = scan_tmp / "output_data_1.txt"

        # ── API fetch ──────────────────────────────────────────────────────
        _prog(10, "fetching")
        if use_cache:
            _pl.info("Loading cached API data …")
            raw = load_cached(TMP_DIR)
            if raw is None:
                raise RuntimeError("No cached data found. Run without use_cache first.")
            flatten_to_lines(raw, flat_file)
        else:
            raw = fetch_all_keywords(keywords, scan_tmp)
            flatten_to_lines(raw, flat_file)
        _prog(60, "fetching")

        # ── Keyword counts ─────────────────────────────────────────────────
        kw_counts: dict = {}
        if raw:
            for kw, offsets_data in raw.items():
                c = {p: 0 for p in PROVIDERS}
                seen: set = set()
                for od in offsets_data.values():
                    for f in od.get("files", []):
                        fid = f.get("id") or f.get("url", "")
                        if fid in seen: continue
                        seen.add(fid)
                        val = f.get("bucket", "") + " " + f.get("url", "")
                        for prov, domain in PROVIDERS.items():
                            if domain in val: c[prov] += 1; break
                kw_counts[kw] = c

        # ── Unique bucket list ─────────────────────────────────────────────
        _prog(65, "unique")
        unique_file = output_dir / f"Unique_{timestamp}.txt"
        build_unique(flat_file, unique_file)

        # ── Per-provider CSVs ──────────────────────────────────────────────
        _prog(75, "providers")
        build_provider_csvs(flat_file, output_dir, timestamp)
        txt    = unique_file.read_text()
        counts = {name: txt.count(domain) for name, domain in PROVIDERS.items()}
        _prog(85, "providers")

        # ── Azure Enumeration ──────────────────────────────────────────────
        ext_counts: dict = {}
        blob_urls:  list = []
        if not skip_azure_enum:
            _prog(87, "azure_enum")
            azure_csv = output_dir / f"Azure_{timestamp}.csv"
            if azure_csv.exists() and azure_csv.stat().st_size > 0:
                ae = enumerate_azure(
                    azure_csv=azure_csv,
                    ext_file=base_dir / EXTENSIONS_FILE,
                    output_dir=output_dir,
                    timestamp=timestamp,
                    tmp_dir=scan_tmp,
                )
                ext_counts = ae.get("ext_counts", {})
                url_file = output_dir / f"URLs_{timestamp}.txt"
                if url_file.exists():
                    blob_urls = [l.strip() for l in url_file.read_text().splitlines() if l.strip()]
            else:
                _pl.warn("Azure CSV empty – skipping enumeration.")

        _prog(98, "summarizing")

        # ── Summary JSON ───────────────────────────────────────────────────
        (output_dir / f"summary_{timestamp}.json").write_text(_json.dumps({
            "timestamp":      timestamp,
            "total_keywords": len(keywords),
            "counts":         counts,
            "total":          sum(counts.values()),
            "extCounts":      ext_counts,
            "keyword_counts": kw_counts,
        }, indent=2))

        total = sum(counts.values())
        _pl.ok(f"AWS: {counts.get('AWS', 0)}")
        _pl.ok(f"Azure: {counts.get('Azure', 0)}")
        _pl.ok(f"GCP: {counts.get('GCP', 0)}")
        _pl.ok(f"DigitalOcean: {counts.get('DigitalOcean', 0)}")
        _pl.ok(f"TOTAL: {total}")
        _pl.banner("Scan Finished")

        comm_q.put({"type": "done", "counts": counts, "ext_counts": ext_counts,
                    "blob_urls": blob_urls[:200], "keyword_counts": kw_counts,
                    "timestamp": timestamp, "output_dir": str(output_dir),
                    "keywords": keywords})

    except Exception as exc:
        _pl.error(f"Scan failed: {exc}")
        comm_q.put({"type": "error", "error": str(exc)})

    finally:
        try: _cfg.API_KEY = orig_key; _cfg.API_OFFSETS = orig_offsets
        except Exception: pass
        try: _sh.rmtree(scan_tmp, ignore_errors=True)
        except Exception: pass


def _bridge_worker(scan_id: str, comm_q) -> None:
    """
    Daemon thread in the main process. Reads typed messages from the child's
    comm_q, updates _active_scans[scan_id]['state'], and feeds the SSE queue.
    """
    scan = _active_scans.get(scan_id)
    if not scan:
        return
    state = scan["state"]
    sse_q = scan["sse_queue"]

    while True:
        try:
            msg = comm_q.get(timeout=30)
        except Exception:
            proc = scan.get("process")
            if proc and not proc.is_alive():
                if state.get("running"):
                    state.update({"running": False, "finished": True,
                                  "error": "Process exited unexpectedly"})
                    sse_q.put({"msg": "__DONE__", "level": "done"})
                break
            continue

        mtype = msg.get("type")
        if mtype == "log":
            sse_q.put({"msg": msg.get("msg", ""), "level": msg.get("level", "info")})
            print(msg.get("msg", ""))

        elif mtype == "progress":
            state["progress"] = msg.get("value", state["progress"])
            if "stage" in msg:
                state["stage"] = msg["stage"]

        elif mtype == "done":
            counts = msg.get("counts", {})
            state.update({"running": False, "finished": True, "error": None,
                          "progress": 100, "stage": "done", "counts": counts,
                          "ext_counts": msg.get("ext_counts", {}),
                          "blob_urls": msg.get("blob_urls", []),
                          "keyword_counts": msg.get("keyword_counts", {}),
                          "timestamp": msg.get("timestamp", state["timestamp"])})
            kws = msg.get("keywords", [])
            _history.append({"ts": msg.get("timestamp", state["timestamp"]),
                             "keywords": len(kws), "kw_sample": kws[0] if kws else "",
                             "counts": counts, "total": sum(counts.values()),
                             "output_dir": msg.get("output_dir", state["output_dir"]),
                             "status": "ok"})
            sse_q.put({"msg": "__DONE__", "level": "done"})
            break

        elif mtype == "error":
            kws = state.get("keywords", [])
            state.update({"running": False, "finished": True,
                          "error": msg.get("error", "Unknown error"), "stage": "error"})
            _history.append({"ts": state["timestamp"], "keywords": len(kws),
                             "kw_sample": kws[0] if kws else "",
                             "counts": {}, "total": 0,
                             "output_dir": state["output_dir"], "status": "error"})
            sse_q.put({"msg": "__DONE__", "level": "done"})
            break


def _start_scan_internal(
    keywords: list,
    api_key: str,
    offsets: list,
    skip_azure_enum: bool = False,
    use_cache: bool = False,
) -> str:
    """Create dirs, spawn child process and bridge thread. Returns scan_id."""
    timestamp  = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_dir = BASE_DIR / "Outputs" / f"{timestamp}_Outputs"
    output_dir.mkdir(parents=True, exist_ok=True)

    scan_id = timestamp
    sse_q   = queue.Queue()
    comm_q  = multiprocessing.Queue()
    state   = {
        "running": True, "finished": False, "error": None,
        "progress": 0, "stage": "starting",
        "timestamp": timestamp, "output_dir": str(output_dir),
        "counts": {}, "ext_counts": {}, "blob_urls": [],
        "keyword_counts": {}, "keywords": keywords,
    }
    _active_scans[scan_id] = {"id": scan_id, "state": state,
                               "sse_queue": sse_q, "process": None}

    proc = multiprocessing.Process(
        target=_run_scan_process,
        args=(keywords, api_key, offsets, skip_azure_enum, use_cache,
              comm_q, timestamp, str(output_dir), str(BASE_DIR)),
        daemon=True,
    )
    proc.start()
    _active_scans[scan_id]["process"] = proc

    threading.Thread(target=_bridge_worker, args=(scan_id, comm_q),
                     daemon=True).start()
    return scan_id


# ═══════════════════════════════════════════════════════════════════════════
# REST API endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/status")
def api_status():
    """Current scan state snapshot. ?scan_id=X returns that scan's state."""
    scan_id = request.args.get("scan_id")
    if scan_id:
        scan = _active_scans.get(scan_id)
        if not scan:
            return jsonify({"error": "Not found"}), 404
        return jsonify(scan["state"])
    # No scan_id → return aggregate view
    running = [
        {"id": sid, **s["state"]}
        for sid, s in _active_scans.items()
        if s["state"].get("running")
    ]
    # Latest finished scan state (for backward-compat with single-scan dashboard)
    latest: dict = {}
    for entry in reversed(_history):
        sc = _active_scans.get(entry.get("ts", ""))
        if sc:
            latest = sc["state"]
            break
    return jsonify({"running_scans": running, "any_running": bool(running), **latest})


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
    """Start a new scan (multiple concurrent scans supported)."""
    data = request.get_json(force=True)
    raw_kws = data.get("keywords", "")
    keywords = [k.strip() for k in raw_kws.splitlines() if k.strip()]
    if not keywords:
        kw_path = BASE_DIR / KEYWORDS_FILE
        if kw_path.exists():
            keywords = [k.strip() for k in kw_path.read_text().splitlines() if k.strip()]
    if not keywords:
        return jsonify({"error": "No keywords provided."}), 400

    api_key         = data.get("api_key", _DEFAULT_API_KEY).strip()
    raw_offsets     = data.get("offsets", "0,1000")
    skip_azure_enum = bool(data.get("skip_azure_enum", False))
    use_cache       = bool(data.get("use_cache", False))

    try:
        offsets = [int(x.strip()) for x in str(raw_offsets).split(",") if x.strip()]
    except ValueError:
        return jsonify({"error": "Offsets must be comma-separated integers."}), 400

    scan_id = _start_scan_internal(keywords, api_key, offsets, skip_azure_enum, use_cache)
    return jsonify({"ok": True, "scan_id": scan_id,
                    "message": f"Scan started with {len(keywords)} keywords."})


@app.route("/api/scan/stop", methods=["POST"])
def api_scan_stop():
    """Terminate a specific scan or all running scans."""
    data = request.get_json(force=True) or {}
    scan_id = data.get("scan_id")
    if scan_id:
        targets = [_active_scans[scan_id]] if scan_id in _active_scans else []
    else:
        targets = [s for s in _active_scans.values() if s["state"].get("running")]
    for scan in targets:
        proc = scan.get("process")
        if proc and proc.is_alive():
            proc.terminate()
        scan["state"].update({"running": False, "finished": True, "error": "Stopped by user"})
        try:
            scan["sse_queue"].put_nowait({"msg": "__DONE__", "level": "done"})
        except Exception:
            pass
    return jsonify({"ok": True, "stopped": len(targets)})


@app.route("/api/logs")
def api_logs_sse():
    """Server-Sent Events stream for a specific scan. Requires ?scan_id=XXX."""
    scan_id = request.args.get("scan_id", "")
    scan = _active_scans.get(scan_id)
    if not scan:
        return jsonify({"error": "scan_id not found"}), 404
    sse_q = scan["sse_queue"]

    def generate():
        yield "data: {\"msg\": \"Connected to log stream.\", \"level\": \"info\"}\n\n"
        while True:
            try:
                item = sse_q.get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("level") == "done":
                    break
            except queue.Empty:
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    api_key = (data.get("api_key") or _DEFAULT_API_KEY).strip()
    sid = datetime.now().strftime("%Y%m%d%H%M%S%f")
    sched: dict = {
        "id": sid, "name": name, "keywords": keywords, "api_key": api_key,
        "enabled": True, "created": datetime.now().isoformat(), "last_run": None,
    }
    if data.get("interval_minutes"):
        try:
            sched["interval_minutes"] = max(1, int(data["interval_minutes"]))
        except (ValueError, TypeError):
            return jsonify({"error": "interval_minutes must be a positive integer"}), 400
    else:
        try:
            sched["interval_hours"] = max(1, int(data.get("interval_hours", 24)))
        except (ValueError, TypeError):
            return jsonify({"error": "interval_hours must be a positive integer"}), 400
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
# AI bucket classification  (optional feature)
# ═══════════════════════════════════════════════════════════════════════════

_AI_CATEGORIES = [
    "finance", "healthcare", "government", "technology",
    "media", "retail", "education", "legal", "personal_data", "general",
]


def _ai_classify_buckets(buckets: list, provider: str, api_key: str, model: str) -> list:
    """
    Classify a list of bucket names using a zero-shot AI model.
    provider: 'huggingface' | 'openai'
    Returns list of {bucket, label, score, scores} dicts.
    """
    import time as _time
    results: list = []

    if provider == "huggingface":
        hf_model = model or "facebook/bart-large-mnli"
        url = f"https://api-inference.huggingface.co/models/{hf_model}"
        headers = {"Authorization": f"Bearer {api_key}"}
        for bucket in buckets:
            try:
                resp = __import__("requests").post(
                    url, headers=headers, timeout=30,
                    json={"inputs": bucket,
                          "parameters": {"candidate_labels": _AI_CATEGORIES,
                                         "multi_label": False}},
                )
                if resp.status_code == 200:
                    r = resp.json()
                    if isinstance(r, dict) and "labels" in r:
                        results.append({
                            "bucket": bucket,
                            "label": r["labels"][0],
                            "score": round(r["scores"][0], 3),
                            "scores": {l: round(s, 3)
                                       for l, s in zip(r["labels"], r["scores"])},
                        })
                    else:
                        results.append({"bucket": bucket, "label": "general",
                                        "score": 0.0, "scores": {}})
                elif resp.status_code == 503:
                    # Model loading – wait and retry once
                    _time.sleep(20)
                    resp2 = __import__("requests").post(
                        url, headers=headers, timeout=30,
                        json={"inputs": bucket,
                              "parameters": {"candidate_labels": _AI_CATEGORIES,
                                             "multi_label": False}},
                    )
                    if resp2.status_code == 200:
                        r = resp2.json()
                        results.append({
                            "bucket": bucket,
                            "label": r.get("labels", ["general"])[0],
                            "score": round(r.get("scores", [0])[0], 3),
                            "scores": {l: round(s, 3)
                                       for l, s in zip(r.get("labels", []),
                                                       r.get("scores", []))},
                        })
                    else:
                        results.append({"bucket": bucket, "label": "error",
                                        "score": 0.0, "scores": {},
                                        "error": f"Model not ready (HTTP {resp2.status_code})"})
                else:
                    results.append({"bucket": bucket, "label": "error", "score": 0.0,
                                    "scores": {},
                                    "error": f"HTTP {resp.status_code}: {resp.text[:120]}"})
            except Exception as exc:
                results.append({"bucket": bucket, "label": "error",
                                 "score": 0.0, "scores": {}, "error": str(exc)})
            _time.sleep(0.3)   # be kind to the free-tier rate limit

    elif provider == "openai":
        oai_model = model or "gpt-4o-mini"
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        cats_str = ", ".join(_AI_CATEGORIES)
        batch_size = 10
        for i in range(0, len(buckets), batch_size):
            batch = buckets[i: i + batch_size]
            prompt = (
                f"Classify each cloud storage bucket name into exactly one category "
                f"from: {cats_str}.\n"
                f"Return ONLY valid JSON: "
                f'{{\"results\": [{{\"bucket\": \"name\", \"label\": \"category\", \"score\": 0.95}}]}}\n\n'
                f"Buckets:\n" + "\n".join(f"- {b}" for b in batch)
            )
            try:
                resp = __import__("requests").post(
                    url, headers=headers, timeout=60,
                    json={"model": oai_model, "temperature": 0,
                          "response_format": {"type": "json_object"},
                          "messages": [{"role": "user", "content": prompt}]},
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    for item in json.loads(content).get("results", []):
                        results.append({
                            "bucket": item.get("bucket", ""),
                            "label": item.get("label", "general"),
                            "score": round(float(item.get("score", 0)), 3),
                            "scores": {},
                        })
                else:
                    for b in batch:
                        results.append({"bucket": b, "label": "error", "score": 0.0,
                                        "scores": {}, "error": f"HTTP {resp.status_code}"})
            except Exception as exc:
                for b in batch:
                    results.append({"bucket": b, "label": "error",
                                    "score": 0.0, "scores": {}, "error": str(exc)})

    return results


@app.route("/api/outputs/<ts>/ai-analyze", methods=["POST"])
def api_ai_analyze(ts: str):
    """Run AI classification on unique bucket names from a completed scan."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Scan not found"}), 404

    data = request.get_json(force=True) or {}
    provider = data.get("provider", "huggingface").lower()
    api_key  = (data.get("api_key") or "").strip()
    model    = (data.get("model") or "").strip()
    limit    = min(int(data.get("limit", 100)), 200)

    if provider not in ("huggingface", "openai"):
        return jsonify({"error": "provider must be 'huggingface' or 'openai'"}), 400
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400

    unique_files = sorted(d.glob("Unique_*.txt"))
    if not unique_files:
        return jsonify({"error": "No unique-buckets file found for this scan. "
                                  "Make sure the scan completed successfully."}), 404

    buckets = [l.strip() for l in unique_files[0].read_text().splitlines()
               if l.strip()][:limit]
    if not buckets:
        return jsonify({"error": "No buckets found in scan output"}), 404

    results = _ai_classify_buckets(buckets, provider, api_key, model)

    category_counts = {cat: sum(1 for r in results if r.get("label") == cat)
                       for cat in _AI_CATEGORIES}
    out = {
        "timestamp": ts,
        "provider": provider,
        "model": model or ("facebook/bart-large-mnli"
                           if provider == "huggingface" else "gpt-4o-mini"),
        "total": len(results),
        "category_counts": category_counts,
        "results": results,
    }
    (d / f"ai_analysis_{ts}.json").write_text(json.dumps(out, indent=2))
    return jsonify(out)


@app.route("/api/outputs/<ts>/ai-results")
def api_ai_results(ts: str):
    """Return cached AI analysis results for a scan (if analysis was run before)."""
    d = _get_output_dir(ts)
    if not d:
        return jsonify({"error": "Not found"}), 404
    files = sorted(d.glob("ai_analysis_*.json"))
    if not files:
        return jsonify({"cached": False, "results": None})
    return jsonify({**json.loads(files[0].read_text()), "cached": True})


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

    if not _DEFAULT_API_KEY:
        print("\n  [WARN] GRAYHATWARFARE_API_KEY is not set.")
        print("         Copy .env.example → .env and add your API key,")
        print("         or export GRAYHATWARFARE_API_KEY=<your_key>\n")

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Cloud Pearser Dashboard")
    print(f"  ─────────────────────────────────────────────")
    print(f"  URL   : {url}")
    print(f"  API   : {'set' if _DEFAULT_API_KEY else 'NOT SET – configure in UI or .env'}")
    print(f"  Sched : {'APScheduler ready' if _APScheduler_available else 'not available (pip install APScheduler)'}")
    print(f"  Stop  : Ctrl+C\n")

    if not args.no_browser:
        # Slight delay so Flask is ready before the browser hits it
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
