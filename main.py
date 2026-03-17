"""
orchestrator.py
───────────────
Single-session devotional generation pipeline.

Phases:
  1. PREFLIGHT     — validate all required files + DB connection + tag coverage
  2. SEED          — tkinter pickers → extract_seed() → reverse-validated seed
  3. SERVER        — launch uvicorn API_Server_Seed in background
  4. GENERATION    — generate_from_seed() → raw devotional JSON
  5. VALIDATION    — validate_devotionals() → production gate
  6. SHUTDOWN      — kill server, print final summary

Gate rules:
  FATAL  → exit immediately, server never started
  ERROR  → exit, server shut down cleanly if running
  WARN   → log and continue
  OK     → proceed

Docker migration path:
  Replace tkinter pickers with CLI args, keep all phase logic intact.
  One substitution — UI layer only.

Usage:
  python orchestrator.py
"""

import atexit
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from tkinter import Tk, filedialog, messagebox, simpledialog

import requests

# =============================================================================
# 1. CONFIG
# =============================================================================

SERVER_URL        = "http://127.0.0.1:50002"
SERVER_MODULE     = "API_Server_Seed:app"
SERVER_PORT       = 50002
SERVER_BOOT_SECS  = 30       # max seconds to wait for server health
HEALTH_ENDPOINT   = f"{SERVER_URL}/docs"

TAG_MISS_WARN_PCT  = 5.0     # warn if miss% below this
TAG_MISS_FATAL_PCT = 20.0    # exit if miss% above this

_SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
TAGS_MASTER_PATH  = os.path.join(_SCRIPT_DIR, "tags_master.json")
BOOK_MAP_PATH     = os.path.join(_SCRIPT_DIR, "book_map.json")

SEP  = "=" * 60
SEP2 = "-" * 60

# =============================================================================
# 2. PHASE REPORTER
# =============================================================================

class PhaseResult:
    OK    = "OK"
    WARN  = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"


def phase_header(n: int, name: str) -> None:
    print(f"\n{SEP}")
    print(f"PHASE {n} — {name}")
    print(SEP)


def ok(msg: str)    -> None: print(f"  ✅  {msg}")
def warn(msg: str)  -> None: print(f"  ⚠️   {msg}")
def error(msg: str) -> None: print(f"  ❌  {msg}")
def info(msg: str)  -> None: print(f"  ·   {msg}")


# =============================================================================
# 3. SERVER MANAGER
# =============================================================================

_server_proc: subprocess.Popen | None = None


def start_server() -> bool:
    """Launch uvicorn in background. Returns True when health check passes."""
    global _server_proc

    info(f"Starting {SERVER_MODULE} on port {SERVER_PORT}...")
    _server_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", SERVER_MODULE,
         "--host", "0.0.0.0", "--port", str(SERVER_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    info(f"Waiting for server (max {SERVER_BOOT_SECS}s)...")
    for i in range(SERVER_BOOT_SECS):
        try:
            r = requests.get(HEALTH_ENDPOINT, timeout=2)
            if r.status_code < 500:
                ok(f"Server ready — {HEALTH_ENDPOINT} ({i+1}s)")
                return True
        except Exception:
            pass
        time.sleep(1)

    error(f"Server did not respond after {SERVER_BOOT_SECS}s")
    return False


def stop_server() -> None:
    """Kill uvicorn subprocess cleanly."""
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        info("Stopping server...")
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()
        ok("Server stopped")
        _server_proc = None


# Register shutdown hook — server killed even on crash or Ctrl+C
atexit.register(stop_server)


def _sig_handler(sig, frame):
    print("\n\nInterrupted — shutting down...")
    stop_server()
    sys.exit(1)

signal.signal(signal.SIGINT,  _sig_handler)
signal.signal(signal.SIGTERM, _sig_handler)


# =============================================================================
# 4. PREFLIGHT
# =============================================================================

def phase_preflight(target_lang: str) -> tuple[str, dict]:
    """
    Validate all required resources before any work starts.
    Returns (PhaseResult, context_dict).
    context_dict contains loaded data needed by later phases.
    """
    phase_header(1, "PREFLIGHT")
    ctx    = {}
    fatal  = False
    warns  = 0

    # Required files
    for label, path in [
        ("tags_master.json", TAGS_MASTER_PATH),
        ("book_map.json",    BOOK_MAP_PATH),
        ("API_Server_Seed.py", os.path.join(_SCRIPT_DIR, "API_Server_Seed.py")),
        ("client_generate_from_seed.py",
         os.path.join(_SCRIPT_DIR, "client_generate_from_seed.py")),
    ]:
        if os.path.exists(path):
            ok(f"{label} found")
        else:
            error(f"{label} NOT FOUND — {path}")
            fatal = True

    if fatal:
        return PhaseResult.FATAL, ctx

    # Load tags master
    with open(TAGS_MASTER_PATH, encoding="utf-8") as f:
        master = json.load(f)
    tags_map    = master["tags"]
    merge_map   = master.get("merge_map", {})
    ctx["tags_map"]   = tags_map
    ctx["merge_map"]  = merge_map
    ok(f"tags_master loaded — {len(tags_map)} keys, {len(merge_map)} merge rules")

    # Language coverage check
    missing_lang = [k for k, v in tags_map.items() if not v.get(target_lang)]
    miss_pct = len(missing_lang) / len(tags_map) * 100 if tags_map else 0

    if miss_pct == 0:
        ok(f"Tag coverage for '{target_lang}': 100%")
    elif miss_pct <= TAG_MISS_WARN_PCT:
        warn(f"Tag coverage for '{target_lang}': {100-miss_pct:.1f}% "
             f"({len(missing_lang)} missing) — continuing")
        warns += 1
    elif miss_pct <= TAG_MISS_FATAL_PCT:
        warn(f"Tag coverage for '{target_lang}': {100-miss_pct:.1f}% "
             f"({len(missing_lang)} missing) — review tags_master before production")
        warns += 1
    else:
        error(f"Tag coverage for '{target_lang}': {100-miss_pct:.1f}% — "
              f"too many missing ({len(missing_lang)}), add translations first")
        return PhaseResult.FATAL, ctx

    # .env / API key
    env_path = os.path.join(_SCRIPT_DIR, ".env")
    if os.path.exists(env_path):
        ok(".env found")
    else:
        warn(".env not found — GOOGLE_API_KEY must be set in environment")
        warns += 1

    result = PhaseResult.WARN if warns else PhaseResult.OK
    print(f"\n  → Preflight: {result}")
    return result, ctx


# =============================================================================
# 5. SEED PHASE
# =============================================================================

def phase_seed(
    ctx: dict,
    kjv_path: str,
    sqlite_path: str,
    output_dir: str,
    target_lang: str,
    target_version: str,
) -> tuple[str, str | None]:
    """
    Run extract_seed() and return (PhaseResult, seed_path | None).
    """
    phase_header(2, "SEED EXTRACTION")

    # Validate DB connection
    try:
        conn   = sqlite3.connect(sqlite_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM verses")
        verse_count = cursor.fetchone()[0]
        conn.close()
        ok(f"SQLite DB connected — {verse_count:,} verses")
    except Exception as e:
        error(f"Cannot open SQLite DB — {e}")
        return PhaseResult.FATAL, None

    # Import and run extract_seed
    sys.path.insert(0, _SCRIPT_DIR)
    from extract_seed import extract_seed

    try:
        seed_path = extract_seed(
            kjv_path, sqlite_path, output_dir,
            target_lang, target_version,
        )
    except SystemExit:
        error("extract_seed exited unexpectedly")
        return PhaseResult.ERROR, None
    except Exception as e:
        error(f"Seed extraction failed — {e}")
        return PhaseResult.ERROR, None

    if not seed_path or not os.path.exists(seed_path):
        error("Seed file not produced")
        return PhaseResult.ERROR, None

    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)

    entry_count = len(seed)
    if entry_count == 0:
        error("Seed is empty — 0 entries extracted")
        return PhaseResult.FATAL, None

    ok(f"Seed ready — {entry_count} entries → {os.path.basename(seed_path)}")

    # Check for violation report
    ts_part      = os.path.basename(seed_path).replace(f"seed_{target_lang}_{target_version}_","").replace(".json","")
    viol_path    = os.path.join(output_dir, f"seed_violations_{target_lang}_{target_version}_{ts_part}.json")
    miss_path    = os.path.join(output_dir, f"seed_tag_misses_{target_lang}_{target_version}_{ts_part}.json")

    result = PhaseResult.OK
    if os.path.exists(viol_path):
        with open(viol_path) as f:
            viols = json.load(f)
        warn(f"{len(viols)} reverse validation violation(s) — review {os.path.basename(viol_path)}")
        result = PhaseResult.WARN

    if os.path.exists(miss_path):
        with open(miss_path) as f:
            misses = json.load(f)
        unique = len({m["en_tag"] for m in misses})
        warn(f"{unique} unique tag miss(es) — EN fallback used, review {os.path.basename(miss_path)}")
        result = PhaseResult.WARN

    print(f"\n  → Seed: {result}")
    return result, seed_path


# =============================================================================
# 6. GENERATION PHASE
# =============================================================================

def phase_generation(
    seed_path: str,
    output_dir: str,
    target_lang: str,
    target_version: str,
) -> tuple[str, str | None]:
    """
    Start server, run generate_from_seed(), return (PhaseResult, raw_path | None).
    """
    phase_header(3, "SERVER STARTUP")

    if not start_server():
        return PhaseResult.FATAL, None

    phase_header(4, "GENERATION")

    sys.path.insert(0, _SCRIPT_DIR)
    from client_generate_from_seed import generate_from_seed

    # Capture raw output path — monkey-patch save_output to intercept
    import client_generate_from_seed as cgfs
    _raw_path = []
    _orig_save = cgfs.save_output

    def _patched_save(completed, lang, version, out_dir):
        path = _orig_save(completed, lang, version, out_dir)
        _raw_path.append(path)
        return path

    cgfs.save_output = _patched_save

    try:
        generate_from_seed(seed_path, target_lang, target_version, output_dir)
    except SystemExit:
        pass
    except Exception as e:
        error(f"Generation failed — {e}")
        cgfs.save_output = _orig_save
        return PhaseResult.ERROR, None
    finally:
        cgfs.save_output = _orig_save

    raw_path = _raw_path[0] if _raw_path else None
    if not raw_path or not os.path.exists(raw_path):
        error("Raw output file not produced")
        return PhaseResult.ERROR, None

    ok(f"Raw output → {os.path.basename(raw_path)}")
    print(f"\n  → Generation: {PhaseResult.OK}")
    return PhaseResult.OK, raw_path


# =============================================================================
# 7. VALIDATION PHASE
# =============================================================================

def phase_validation(raw_path: str, target_lang: str) -> str:
    """
    Run basic quality validation on the raw output.
    Returns PhaseResult.
    """
    phase_header(5, "VALIDATION")

    def has_devanagari(text):
        return bool(re.search(r'[\u0900-\u097F]', text))

    try:
        with open(raw_path, encoding="utf-8") as f:
            data = json.load(f)

        lang_data = data.get("data", {}).get(target_lang, {})
        entries   = {date: items[0] for date, items in lang_data.items() if items}
        total     = len(entries)

        if total == 0:
            error("No entries found in output")
            return PhaseResult.ERROR

        issues        = []
        script_fails  = []
        no_amen       = []
        reflexion_lens = []

        for date, entry in entries.items():
            r = entry.get("reflexion", "")
            o = entry.get("oracion",   "")
            reflexion_lens.append(len(r))

            if target_lang == "hi":
                if not has_devanagari(r): script_fails.append((date, "reflexion"))
                if not has_devanagari(o): script_fails.append((date, "oracion"))
            if not any(e in o for e in ["آمین", "आमीन", "अमीन", "Amén", "Amen", "アーメン", "阿门", "아멘"]):
                no_amen.append(date)
            for key in ("id","date","language","version","versiculo","reflexion","para_meditar","oracion","tags"):
                if key not in entry:
                    issues.append(f"[{date}] missing: {key}")

        avg_r = sum(reflexion_lens) // total if total else 0
        ok(f"{total} entries validated")
        ok(f"Reflexion avg: {avg_r} chars")

        if script_fails:
            for date, field in script_fails[:5]:
                warn(f"[{date}] {field} script check failed")
        else:
            ok("Script validation: all pass")

        if no_amen:
            warn(f"{len(no_amen)} entries missing amen ending")
        else:
            ok("Amen endings: all present")

        if issues:
            for i in issues[:5]:
                warn(i)
        else:
            ok("Structure: all keys present")

        result = PhaseResult.WARN if (script_fails or no_amen or issues) else PhaseResult.OK
        print(f"\n  → Validation: {result}")
        return result

    except Exception as e:
        error(f"Validation failed — {e}")
        return PhaseResult.ERROR


# =============================================================================
# 8. TKINTER UI
# =============================================================================

def collect_inputs() -> dict | None:
    """
    Collect all pipeline inputs via tkinter dialogs.
    Returns dict of inputs or None if cancelled.
    Docker migration: replace this function with argparse.
    """
    root = Tk()
    root.withdraw()

    messagebox.showinfo(
        "Devotional Pipeline — Step 1 of 5",
        "Select the KJV devotional JSON file.\n\n"
        "Skip this step if you already have a seed file."
    )

    kjv_path = filedialog.askopenfilename(
        title="Select KJV devotional JSON (or cancel to use existing seed)",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
    )

    seed_path = None
    sqlite_path = None

    if not kjv_path:
        messagebox.showinfo(
            "Devotional Pipeline — Step 1b of 5",
            "Select an existing seed JSON file."
        )
        seed_path = filedialog.askopenfilename(
            title="Select existing seed JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not seed_path:
            messagebox.showerror("Cancelled", "No KJV file or seed file selected.")
            root.destroy()
            return None
    else:
        messagebox.showinfo(
            "Devotional Pipeline — Step 2 of 5",
            "Select the SQLite Bible database."
        )
        sqlite_path = filedialog.askopenfilename(
            title="Select SQLite Bible DB",
            filetypes=[("SQLite files", "*.SQLite3 *.db *.sqlite"), ("All files", "*.*")],
        )
        if not sqlite_path:
            root.destroy()
            return None

    target_lang = simpledialog.askstring(
        "Devotional Pipeline — Step 3 of 5",
        "Target language code (e.g. hi, es, de, ko):",
        initialvalue="hi",
    )
    if not target_lang:
        root.destroy()
        return None

    target_version = simpledialog.askstring(
        "Devotional Pipeline — Step 4 of 5",
        "Bible version code (e.g. OV, HERV, KJV):",
        initialvalue="OV",
    )
    if not target_version:
        root.destroy()
        return None

    messagebox.showinfo(
        "Devotional Pipeline — Step 5 of 5",
        "Select the output folder."
    )
    output_dir = filedialog.askdirectory(title="Select output folder")
    if not output_dir:
        root.destroy()
        return None

    root.destroy()

    return {
        "kjv_path":       kjv_path or None,
        "seed_path":      seed_path,
        "sqlite_path":    sqlite_path,
        "target_lang":    target_lang.strip().lower(),
        "target_version": target_version.strip().upper(),
        "output_dir":     output_dir,
    }


# =============================================================================
# 9. SUMMARY
# =============================================================================

def print_summary(phases: dict, start_time: float) -> None:
    elapsed = time.time() - start_time
    mins, secs = divmod(int(elapsed), 60)

    print(f"\n{SEP}")
    print("PIPELINE SUMMARY")
    print(SEP)
    for name, result in phases.items():
        icon = {"OK":"✅","WARN":"⚠️ ","ERROR":"❌","FATAL":"🚫","SKIP":"⏭️ "}.get(result,"·")
        print(f"  {icon}  {name:<20} {result}")
    print(f"\n  Elapsed: {mins}m {secs}s")
    print(SEP)


# =============================================================================
# 10. MAIN
# =============================================================================

def main() -> None:
    start_time = time.time()
    phases     = {}

    print(f"\n{SEP}")
    print("DEVOTIONAL GENERATION PIPELINE")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)

    # ── Collect inputs ────────────────────────────────────────────────────────
    inputs = collect_inputs()
    if not inputs:
        print("Cancelled.")
        sys.exit(0)

    lang    = inputs["target_lang"]
    version = inputs["target_version"]
    info(f"Lang: {lang} | Version: {version}")
    info(f"Output: {inputs['output_dir']}")

    # ── Phase 1: Preflight ────────────────────────────────────────────────────
    preflight_result, ctx = phase_preflight(lang)
    phases["Preflight"] = preflight_result
    if preflight_result == PhaseResult.FATAL:
        print_summary(phases, start_time)
        sys.exit(1)

    # ── Phase 2: Seed ─────────────────────────────────────────────────────────
    seed_path = inputs.get("seed_path")

    if seed_path:
        # Skip extraction — use existing seed
        phase_header(2, "SEED EXTRACTION")
        info(f"Using existing seed: {os.path.basename(seed_path)}")
        with open(seed_path, encoding="utf-8") as f:
            count = len(json.load(f))
        ok(f"{count} entries found")
        phases["Seed"] = PhaseResult.OK + " (skipped)"
    else:
        seed_result, seed_path = phase_seed(
            ctx,
            inputs["kjv_path"],
            inputs["sqlite_path"],
            inputs["output_dir"],
            lang,
            version,
        )
        phases["Seed"] = seed_result
        if seed_result in (PhaseResult.FATAL, PhaseResult.ERROR):
            print_summary(phases, start_time)
            sys.exit(1)

    # ── Phases 3+4: Server + Generation ──────────────────────────────────────
    gen_result, raw_path = phase_generation(
        seed_path,
        inputs["output_dir"],
        lang,
        version,
    )
    phases["Server"]     = PhaseResult.OK if _server_proc else PhaseResult.ERROR
    phases["Generation"] = gen_result

    if gen_result in (PhaseResult.FATAL, PhaseResult.ERROR):
        stop_server()
        print_summary(phases, start_time)
        sys.exit(1)

    # ── Phase 5: Validation ───────────────────────────────────────────────────
    val_result = phase_validation(raw_path, lang)
    phases["Validation"] = val_result

    # ── Shutdown + Summary ────────────────────────────────────────────────────
    stop_server()
    print_summary(phases, start_time)

    if val_result == PhaseResult.OK:
        print(f"\n  🎉  Pipeline complete — {os.path.basename(raw_path)} ready for patch + validate\n")
    else:
        print(f"\n  ⚠️   Pipeline complete with warnings — review reports before promoting\n")


if __name__ == "__main__":
    main()
