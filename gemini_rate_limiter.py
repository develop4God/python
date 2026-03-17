"""
gemini_rate_limiter.py
──────────────────────
Single responsibility: enforce Gemini API rate limits.

All model limits and safety margins are defined here.
Callers (API_Server_Seed.py, API_Server_V9.py, etc.) call acquire()
before every Gemini request — they know nothing about the limits.

Supported models:
  - gemini-2.0-flash       RPM:15  RPD:200
  - gemini-2.5-flash-lite  RPM:15  RPD:20
  - gemini-1.5-flash       RPM:15  RPD:1500

Usage:
    from gemini_rate_limiter import GeminiRateLimiter

    limiter = GeminiRateLimiter(model="gemini-2.0-flash")
    limiter.acquire()   # blocks until safe to call
"""

import threading
import time


# ── Model registry ────────────────────────────────────────────────────────────
# Source: https://ai.google.dev/gemini-api/docs/rate-limits (free tier)
# RPM  = requests per minute
# RPD  = requests per day  (None = no enforced daily limit)
MODEL_LIMITS = {
    "gemini-2.0-flash": {
        "RPM": 15,
        "RPD": 200,
        "display": "Gemini 2.0 Flash (free tier)",
    },
    "gemini-2.5-flash": {
        "RPM": 15,
        "RPD": 2000,
        "display": "Gemini 2.5 Flash Lite (free tier)",
    },
    "gemini-1.5-flash": {
        "RPM": 15,
        "RPD": 1500,
        "display": "Gemini 1.5 Flash (free tier)",
    },
}

# Safety margin applied to all limits (stay at 80% to avoid edge cases)
SAFETY_MARGIN = 0.8

# Rolling window for RPM tracking (seconds)
RPM_WINDOW_SECS = 60

# How long to wait when RPM limit is hit (slightly over window to ensure reset)
RPM_WAIT_SECS = 65

# How long to wait when RPD limit is hit (in seconds — 1 hour, then recheck)
RPD_WAIT_SECS = 3600


class GeminiRateLimiterError(RuntimeError):
    """Raised when rate limit cannot be resolved (e.g. daily quota exhausted)."""


class GeminiRateLimiter:
    """
    Thread-safe Gemini API rate limiter.

    Tracks RPM via in-memory rolling window (no DB needed).
    Tracks RPD via in-memory daily counter (resets at UTC midnight).

    All limit values come from MODEL_LIMITS — callers define nothing.
    """

    def __init__(self, model: str = "gemini-2.0-flash", safety_margin: float = SAFETY_MARGIN):
        if model not in MODEL_LIMITS:
            raise ValueError(
                f"Unknown model: '{model}'. "
                f"Available: {list(MODEL_LIMITS.keys())}"
            )

        self._model   = model
        self._limits  = MODEL_LIMITS[model]
        self._safe_rpm = int(self._limits["RPM"] * safety_margin)
        self._safe_rpd = int(self._limits["RPD"] * safety_margin) if self._limits.get("RPD") else None

        self._lock          = threading.Lock()
        self._recent_calls  = []   # UTC timestamps of calls in last RPM_WINDOW_SECS
        self._daily_calls   = 0
        self._day_start_utc = self._utc_midnight()

        print(
            f"INFO: GeminiRateLimiter initialized — "
            f"model: {model} | "
            f"RPM safe: {self._safe_rpm}/{self._limits['RPM']} | "
            f"RPD safe: {self._safe_rpd}/{self._limits['RPD']}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def acquire(self) -> None:
        """
        Block until it is safe to make a Gemini API call.
        Raises GeminiRateLimiterError if daily quota is exhausted.
        """
        with self._lock:
            while True:
                now = time.time()
                self._reset_daily_if_needed(now)
                self._purge_old_calls(now)

                rpm = len(self._recent_calls)
                rpd = self._daily_calls

                print(
                    f"INFO: Rate — RPM:{rpm}/{self._safe_rpm} "
                    + (f"RPD:{rpd}/{self._safe_rpd}" if self._safe_rpd else "")
                )

                # ── RPD check ─────────────────────────────────────────────────
                if self._safe_rpd and rpd >= self._safe_rpd:
                    raise GeminiRateLimiterError(
                        f"Daily quota reached ({rpd} calls today, safe limit: {self._safe_rpd}). "
                        f"Resets at UTC midnight."
                    )

                # ── RPM check ─────────────────────────────────────────────────
                if rpm >= self._safe_rpm:
                    print(f"INFO: RPM limit reached — waiting {RPM_WAIT_SECS}s...")
                    time.sleep(RPM_WAIT_SECS)
                    continue

                # ── Safe to proceed ───────────────────────────────────────────
                self._recent_calls.append(now)
                self._daily_calls += 1
                break

    def status(self) -> dict:
        """Returns current rate status snapshot — useful for logging."""
        with self._lock:
            now = time.time()
            self._reset_daily_if_needed(now)
            self._purge_old_calls(now)
            return {
                "model":     self._model,
                "rpm":       len(self._recent_calls),
                "rpm_safe":  self._safe_rpm,
                "rpm_limit": self._limits["RPM"],
                "rpd":       self._daily_calls,
                "rpd_safe":  self._safe_rpd,
                "rpd_limit": self._limits["RPD"],
            }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _purge_old_calls(self, now: float) -> None:
        """Remove calls outside the rolling RPM window."""
        cutoff = now - RPM_WINDOW_SECS
        self._recent_calls = [t for t in self._recent_calls if t >= cutoff]

    def _reset_daily_if_needed(self, now: float) -> None:
        """Reset daily counter at UTC midnight."""
        midnight = self._utc_midnight()
        if midnight > self._day_start_utc:
            self._daily_calls   = 0
            self._day_start_utc = midnight
            print("INFO: Daily call counter reset (UTC midnight)")

    @staticmethod
    def _utc_midnight() -> float:
        """Returns the UTC timestamp of today's midnight."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
