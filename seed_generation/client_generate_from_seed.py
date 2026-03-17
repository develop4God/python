"""
generate_from_seed.py — Seed-driven devotional generator.
Works with API_Server_Seed.py.

Python owns: DevotionalBuilder, tags, ID, versiculo field,
             para_meditar, checkpoint, final JSON structure.
Gemini provides: reflexion + oracion only.
"""

import json, os, re, signal, sys, time, requests
from datetime import datetime
from tkinter import Tk, filedialog, messagebox, simpledialog

API_URL             = "http://127.0.0.1:50002/generate_creative"
REQUEST_TIMEOUT     = 300
DELAY_BETWEEN       = 1
CHECKPOINT_INTERVAL = 1

_SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
CHECKPOINT_FILE = os.path.join(_SCRIPT_DIR, "generate_seed_checkpoint.json")


# =============================================================================
# DEVOTIONAL BUILDER
# =============================================================================

class DevotionalValidationError(ValueError):
    pass


class DevotionalBuilder:


    def __init__(self, date_key, seed_entry, master_lang, master_version):
        self._date      = date_key
        self._seed      = seed_entry
        self._lang      = master_lang
        self._version   = master_version
        self._reflexion = ""
        self._oracion   = ""

    def merge(self, reflexion, oracion):
        self._reflexion = reflexion.strip()
        self._oracion   = oracion.strip()
        return self

    def _build_versiculo(self):
        cita  = self._seed["versiculo"]["cita"]
        texto = self._seed["versiculo"]["texto"]
        return cita + " " + self._version + ': "' + texto + '"'

    def _build_id(self):
        cita         = self._seed["versiculo"]["cita"]
        id_part      = re.sub(r"\s+", "", cita).replace(":", "")
        date_compact = self._date.replace("-", "")
        return id_part + self._version + date_compact

    def _extract_tags(self) -> list:
        """
        Read pre-translated tags directly from seed flat array.
        Seed produces: "tags": ["यीशु", "मनन"]  (target language only)
        Falls back to ["devotional", "fe"] if missing.
        """
        tags = self._seed.get("tags", [])
        if isinstance(tags, list) and tags:
            return tags
        return ["devotional", "fe"]

    def validate(self):
        errors = []
        if not self._reflexion:               errors.append("reflexion empty")
        if not self._oracion:                 errors.append("oracion empty")
        if not self._seed.get("versiculo", {}).get("cita"):   errors.append("cita missing")
        if not self._seed.get("versiculo", {}).get("texto"):  errors.append("texto missing")
        if not self._seed.get("para_meditar"):                errors.append("para_meditar empty")
        if errors:
            raise DevotionalValidationError("[" + self._date + "] " + "; ".join(errors))

    def build(self):
        self.validate()
        return {
            "id":           self._build_id(),
            "date":         self._date,
            "language":     self._lang,
            "version":      self._version,
            "versiculo":    self._build_versiculo(),
            "reflexion":    self._reflexion,
            "para_meditar": self._seed["para_meditar"],
            "oracion":      self._oracion,
            "tags":         self._extract_tags(),
        }


# =============================================================================
# CHECKPOINT
# =============================================================================

def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, encoding="utf-8") as f:
                data = json.load(f)
            print("INFO: Checkpoint found — " + str(data["completed_count"]) + " dates done")
            return data
        except Exception as e:
            print("WARNING: Could not load checkpoint: " + str(e))
    return None


def save_checkpoint(completed, count, seed_path, lang, version):
    data = {
        "completed":       completed,
        "completed_count": count,
        "seed_path":       seed_path,
        "master_lang":     lang,
        "master_version":  version,
        "timestamp":       datetime.now().isoformat()
    }
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("  checkpoint saved — " + str(count) + " completed")


def delete_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            os.remove(CHECKPOINT_FILE)
        except Exception:
            pass


# =============================================================================
# OUTPUT
# =============================================================================

def save_output(completed, lang, version, output_dir):
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = "raw_" + lang + "_" + version + "_" + ts + ".json"
    path     = os.path.join(output_dir, filename)
    nested   = {lang: {date: [devo] for date, devo in completed.items()}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"data": nested}, f, ensure_ascii=False, indent=2)
    return path


# =============================================================================
# GENERATION
# =============================================================================

def generate_from_seed(seed_path, master_lang, master_version, output_dir):
    SEP = "=" * 60
    print("\n" + SEP)
    print("SEED-DRIVEN GENERATOR")
    print(SEP)
    print("  Seed    : " + seed_path)
    print("  Lang    : " + master_lang + "  Version: " + master_version)
    print("  Server  : " + API_URL)
    print("  Output  : " + output_dir)
    print(SEP + "\n")

    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)

    all_dates   = sorted(seed.keys())
    total       = len(all_dates)
    print("INFO: " + str(total) + " seed entries\n")

    completed   = {}
    start_index = 0
    checkpoint  = load_checkpoint()

    if checkpoint and checkpoint.get("seed_path") == seed_path:
        ans = input("\nCheckpoint — " + str(checkpoint["completed_count"]) + " done. Resume? (y/n): ").strip().lower()
        if ans == "y":
            completed    = checkpoint["completed"]
            start_index  = checkpoint["completed_count"]
            print("INFO: Resuming from " + str(start_index + 1) + "/" + str(total) + "\n")

    interrupted = False

    def _sig(sig, frame):
        nonlocal interrupted
        print("\n\nCtrl+C — saving checkpoint...")
        interrupted = True

    signal.signal(signal.SIGINT, _sig)

    success_count = start_index
    error_count   = 0
    error_dates   = []

    print("INFO: Checkpoint every " + str(CHECKPOINT_INTERVAL) + " successes")
    print("-" * 60)

    for i in range(start_index, total):
        if interrupted:
            break

        date_key   = all_dates[i]
        seed_entry = seed[date_key]
        cita       = seed_entry["versiculo"]["cita"]

        print("\n[" + str(i+1) + "/" + str(total) + "] " + date_key + " — " + cita)

        payload = {
            "date":           date_key,
            "master_lang":    master_lang,
            "master_version": master_version,
            "versiculo_cita": cita,
            "topic":          None,
        }

        try:
            resp = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 422:
                detail = resp.json().get("detail", "")
                if "SCRIPT_ERROR" in detail:
                    print("  SKIPPED — script error: " + detail)
                    error_count += 1
                    error_dates.append({"date": date_key, "cita": cita, "reason": detail})
                    time.sleep(DELAY_BETWEEN)
                    continue

            if resp.status_code == 503:
                # 3s first — 90% of the time this resolves it
                # if still failing by 60s the server is truly down
                resolved = False
                for attempt, wait in enumerate([3, 15, 30, 60], start=1):
                    print(f"  503 — attempt {attempt}/4, waiting {wait}s...")
                    time.sleep(wait)
                    resp = requests.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
                    if resp.status_code == 200:
                        resolved = True
                        print(f"  ↩ resolved on attempt {attempt + 1}")
                        break

                if not resolved:
                    # Save checkpoint at current date so resume retries same verse
                    print(f"  503 exhausted — saving checkpoint at [{date_key}] ({cita})")
                    save_checkpoint(completed, success_count, seed_path, master_lang, master_version)
                    error_dates.append({"date": date_key, "cita": cita,
                                        "reason": "503 exhausted 4 retries — checkpoint saved, resume to retry same verse"})
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ep = os.path.join(output_dir, "generation_errors_" + master_lang + "_" + master_version + "_" + ts + ".json")
                    with open(ep, "w", encoding="utf-8") as f:
                        json.dump(error_dates, f, ensure_ascii=False, indent=2)
                    print(f"  Errors -> {ep}")
                    print("  Server appears down. Run again to resume from this date.")
                    sys.exit(1)

            resp.raise_for_status()
            data      = resp.json()
            reflexion = data.get("reflexion", "").strip()
            oracion   = data.get("oracion",   "").strip()

            try:
                builder    = DevotionalBuilder(date_key, seed_entry, master_lang, master_version)
                devotional = builder.merge(reflexion, oracion).build()
                completed[date_key] = devotional
                success_count += 1
                print("  OK — " + str(len(reflexion)) + " chars | tags: " + str(devotional["tags"]))
                if success_count % CHECKPOINT_INTERVAL == 0:
                    save_checkpoint(completed, success_count, seed_path, master_lang, master_version)
            except DevotionalValidationError as e:
                print("  Validation error: " + str(e))
                error_count += 1
                error_dates.append({"date": date_key, "cita": cita, "reason": str(e)})

        except requests.exceptions.ConnectionError:
            msg = "Cannot reach server — is API_Server_Seed.py running?"
            print("  " + msg)
            error_dates.append({"date": date_key, "cita": cita, "reason": msg})
            break
        except Exception as e:
            msg = type(e).__name__ + ": " + str(e)
            print("  Error: " + msg)
            error_count += 1
            error_dates.append({"date": date_key, "cita": cita, "reason": msg})

        time.sleep(DELAY_BETWEEN)

    if interrupted and completed:
        save_checkpoint(completed, success_count, seed_path, master_lang, master_version)
        print("\nProgress saved. Run again to resume.")
        sys.exit(0)

    print("\n" + "-" * 60)

    if completed:
        out = save_output(completed, master_lang, master_version, output_dir)
        print("\nOutput  -> " + out)
        print("Next    -> patch_devotional_verses.py --json " + out)
        delete_checkpoint()
        if error_dates:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            ep = os.path.join(output_dir, "generation_errors_" + master_lang + "_" + master_version + "_" + ts + ".json")
            with open(ep, "w", encoding="utf-8") as f:
                json.dump(error_dates, f, ensure_ascii=False, indent=2)
            print("Errors  -> " + ep)
    else:
        print("No devotionals generated.")

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("  Seed entries  : " + str(total))
    print("  Generated OK  : " + str(success_count))
    print("  Skipped       : " + str(error_count))
    print("=" * 60 + "\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

def main():
    root = Tk()
    root.withdraw()

    messagebox.showinfo("Seed Generator 1/4", "Select the seed JSON file.")
    seed_path = filedialog.askopenfilename(
        title="Select seed JSON",
        filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
    )
    if not seed_path:
        sys.exit(0)

    dlang = "hi"
    dver  = "OV"

    master_lang = simpledialog.askstring("Language", "Language code (e.g. hi, es):", initialvalue=dlang)
    if not master_lang:
        sys.exit(0)

    master_version = simpledialog.askstring("Version", "Version code (e.g. OV, HERV):", initialvalue=dver)
    if not master_version:
        sys.exit(0)

    messagebox.showinfo("Seed Generator 4/4", "Select the output folder.")
    output_dir = filedialog.askdirectory(title="Select output folder")
    if not output_dir:
        sys.exit(0)

    root.destroy()
    generate_from_seed(seed_path, master_lang, master_version, output_dir)


if __name__ == "__main__":
    main()
