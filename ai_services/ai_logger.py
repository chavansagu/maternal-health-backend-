"""
AI Reports - Structured Logging & Performance Tracing Utility

Provides:
- Structured log format: [AI-REPORT] [request_id=X] [step=Y] [status=Z] [time_ms=N]
- StepTimer context manager for per-step timing
- Performance threshold warnings
- Safe error logging (no sensitive data in messages)
"""
import logging
import time
import os
from contextlib import contextmanager
from typing import Optional, Generator

# ── Logger setup ─────────────────────────────────────────────────────────────
# Uses the root "ai_reports" logger so output goes through the app's
# existing logging configuration (uvicorn, file handler, etc.)
logger = logging.getLogger("ai_reports")

# ── Performance thresholds (seconds) ─────────────────────────────────────────
_THRESHOLD_LLM_WARN    = float(os.getenv("AI_LOG_LLM_WARN_SEC",   "10"))
_THRESHOLD_DB_WARN     = float(os.getenv("AI_LOG_DB_WARN_SEC",     "3"))
_THRESHOLD_TOTAL_WARN  = float(os.getenv("AI_LOG_TOTAL_WARN_SEC",  "15"))

# ── Debug timing flag ─────────────────────────────────────────────────────────
AI_DEBUG = os.getenv("AI_DEBUG", "false").lower() == "true"


def _fmt(request_id: str, step: str, status: str, time_ms: Optional[int] = None, extra: str = "") -> str:
    """Build the standard log line."""
    parts = [
        "[AI-REPORT]",
        f"[request_id={request_id}]",
        f"[step={step}]",
        f"[status={status}]",
    ]
    if time_ms is not None:
        parts.append(f"[time_ms={time_ms}]")
    if extra:
        parts.append(extra)
    return " ".join(parts)


def log_start(request_id: str, step: str, extra: str = "") -> None:
    """Log the start of a pipeline step."""
    logger.info(_fmt(request_id, step, "START", extra=extra))


def log_success(request_id: str, step: str, time_ms: int, extra: str = "") -> None:
    """Log successful completion of a pipeline step."""
    logger.info(_fmt(request_id, step, "SUCCESS", time_ms, extra))


def log_warning(request_id: str, step: str, time_ms: int, message: str) -> None:
    """Log a performance warning for a pipeline step."""
    logger.warning(_fmt(request_id, step, "SLOW", time_ms, f"[warn={message}]"))


def log_error(request_id: str, step: str, time_ms: int, error: str) -> None:
    """Log a step failure. error is the exception type + message only — no stack trace in message."""
    logger.error(_fmt(request_id, step, "ERROR", time_ms, f"[error={error}]"))


def log_request_start(request_id: str, user_id: int, role: str, question_len: int) -> None:
    """Log the beginning of a full AI report request."""
    logger.info(
        _fmt(request_id, "REQUEST_START", "START",
             extra=f"[user_id={user_id}] [role={role}] [question_chars={question_len}]")
    )


def log_request_complete(request_id: str, total_ms: int, result_count: int, provider: str) -> None:
    """Log successful completion of a full AI report request."""
    msg = _fmt(
        request_id, "REQUEST_COMPLETE", "SUCCESS", total_ms,
        f"[rows={result_count}] [provider={provider}]"
    )
    logger.info(msg)
    if total_ms / 1000 > _THRESHOLD_TOTAL_WARN:
        logger.warning(
            _fmt(request_id, "REQUEST_COMPLETE", "SLOW", total_ms,
                 f"[warn=Total request exceeded {_THRESHOLD_TOTAL_WARN}s]")
        )


def log_request_failed(request_id: str, total_ms: int, step: str, error: str) -> None:
    """Log a failed AI report request."""
    logger.error(
        _fmt(request_id, "REQUEST_FAILED", "ERROR", total_ms,
             f"[failed_step={step}] [error={error}]")
    )


@contextmanager
def timed_step(request_id: str, step: str, extra_start: str = "") -> Generator[dict, None, None]:
    """
    Context manager that times a pipeline step and logs start/success/error.

    Usage:
        with timed_step(request_id, "LLM_CALL") as t:
            result = engine.generate_sql(...)
        # t["ms"] holds the elapsed milliseconds after the block

    The yielded dict has key "ms" populated after the block exits.
    On exception: logs ERROR and re-raises — does NOT suppress.
    """
    timing: dict = {"ms": 0}
    log_start(request_id, step, extra_start)
    t0 = time.perf_counter()
    try:
        yield timing
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        timing["ms"] = elapsed_ms
        log_success(request_id, step, elapsed_ms)
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        timing["ms"] = elapsed_ms
        log_error(request_id, step, elapsed_ms, f"{type(exc).__name__}: {exc}")
        raise


def check_llm_threshold(request_id: str, elapsed_ms: int) -> None:
    """Emit a warning if LLM call exceeded threshold."""
    if elapsed_ms / 1000 > _THRESHOLD_LLM_WARN:
        log_warning(request_id, "LLM_CALL", elapsed_ms,
                    f"LLM response exceeded {_THRESHOLD_LLM_WARN}s")


def check_db_threshold(request_id: str, elapsed_ms: int) -> None:
    """Emit a warning if DB execution exceeded threshold."""
    if elapsed_ms / 1000 > _THRESHOLD_DB_WARN:
        log_warning(request_id, "DB_EXECUTE", elapsed_ms,
                    f"DB query exceeded {_THRESHOLD_DB_WARN}s")
