"""
Google Sheets MCP v2 — Infrastructure Layer

Provides: errors, caching, logging, validation, context, range parsing,
table intelligence, safety helpers, response standardization.
"""

from __future__ import annotations

import re
import statistics
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional


# ═════════════════════════════════════════════════════════════════════════════
# CUSTOM ERROR CLASSES
# ═════════════════════════════════════════════════════════════════════════════

class SheetError(Exception):
    """Base exception for Sheets MCP."""
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class SheetNotFoundError(SheetError):
    """Spreadsheet or tab not found."""
    pass


class SheetAmbiguityError(SheetError):
    """Multiple spreadsheets match the reference."""
    def __init__(self, message: str, matches: list = None):
        super().__init__(message)
        self.matches = matches or []


class SheetPermissionError(SheetError):
    """Permission denied on spreadsheet."""
    pass


class SheetRateLimitError(SheetError):
    """API rate limit hit."""
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


class SheetValidationError(SheetError):
    """Invalid arguments."""
    def __init__(self, param: str, value: Any, reason: str):
        super().__init__(f"Invalid value for '{param}': {value} — {reason}")
        self.param = param
        self.value = value
        self.reason = reason


class SheetRangeError(SheetError):
    """Invalid or unresolvable range reference."""
    pass


class SheetSafetyError(SheetError):
    """Safety check failed — destructive operation requires confirmation."""
    pass


# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

MAX_ROWS_PER_READ    = 1000   # max rows returned per paginated read
LARGE_DATA_THRESHOLD = 500    # rows — warn when overwriting more than this
BATCH_ROW_LIMIT      = 1000   # max rows in a single bulk append/delete
BATCH_RANGE_LIMIT    = 20     # max ranges in a single batchUpdate call


# ═════════════════════════════════════════════════════════════════════════════
# TTL CACHE
# ═════════════════════════════════════════════════════════════════════════════

class SimpleCache:
    """Thread-safe in-memory TTL cache."""

    def __init__(self, default_ttl: int = 300):
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._store:
                self._misses += 1
                return None
            value, expiry = self._store[key]
            if time.time() > expiry:
                del self._store[key]
                self._misses += 1
                return None
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int = None) -> None:
        with self._lock:
            self._store[key] = (value, time.time() + (ttl or self._default_ttl))

    def invalidate(self, prefix: str = "") -> int:
        with self._lock:
            if not prefix:
                count = len(self._store)
                self._store.clear()
                return count
            keys = [k for k in self._store if k.startswith(prefix)]
            for k in keys:
                del self._store[k]
            return len(keys)

    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._store),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 2) if total else 0,
            }


_sheets_cache = SimpleCache(default_ttl=300)


def cached(namespace: str, ttl: int = 300):
    """Decorator — cache function result keyed by namespace + args."""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            parts = [namespace, func.__name__] + [str(a) for a in args]
            parts += [f"{k}={v}" for k, v in sorted(kwargs.items())]
            key = ":".join(parts)
            hit = _sheets_cache.get(key)
            if hit is not None:
                return hit
            result = func(*args, **kwargs)
            _sheets_cache.set(key, result, ttl)
            return result
        return wrapper
    return decorator


def invalidate_cache(prefix: str = "") -> dict:
    count = _sheets_cache.invalidate(prefix)
    return {"invalidated": count, "prefix": prefix or "all"}


# ═════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCallLog:
    tool_name: str
    timestamp: datetime
    latency_ms: float
    success: bool
    error: Optional[str] = None
    args_preview: str = ""


class SheetsLogger:
    """Per-tool structured logger."""

    def __init__(self, max_history: int = 100):
        self._calls: list[ToolCallLog] = []
        self._lock = threading.Lock()
        self._max_history = max_history

    def log(self, tool_name: str, latency_ms: float, success: bool,
            error: str = None, args_preview: str = "") -> None:
        with self._lock:
            self._calls.append(ToolCallLog(
                tool_name=tool_name,
                timestamp=datetime.now(),
                latency_ms=latency_ms,
                success=success,
                error=error,
                args_preview=args_preview[:200],
            ))
            if len(self._calls) > self._max_history:
                self._calls = self._calls[-self._max_history:]

    def get_stats(self) -> dict:
        with self._lock:
            if not self._calls:
                return {"total_calls": 0}
            total = len(self._calls)
            successful = sum(1 for c in self._calls if c.success)
            avg_ms = sum(c.latency_ms for c in self._calls) / total
            counts: dict[str, int] = {}
            for c in self._calls:
                counts[c.tool_name] = counts.get(c.tool_name, 0) + 1
            return {
                "total_calls": total,
                "success_rate": round(successful / total, 2),
                "avg_latency_ms": round(avg_ms, 2),
                "tool_breakdown": counts,
            }

    def get_tool_stats(self) -> dict:
        with self._lock:
            stats: dict[str, dict] = {}
            for c in self._calls:
                s = stats.setdefault(c.tool_name, {"calls": 0, "success": 0, "total_ms": 0.0})
                s["calls"] += 1
                s["total_ms"] += c.latency_ms
                if c.success:
                    s["success"] += 1
            return {
                name: {
                    "calls": v["calls"],
                    "success_rate": f"{v['success'] / v['calls'] * 100:.1f}%",
                    "avg_latency_ms": round(v["total_ms"] / v["calls"], 2),
                }
                for name, v in stats.items()
            }

    def get_recent_errors(self, n: int = 5) -> list[dict]:
        with self._lock:
            errors = [c for c in self._calls if not c.success]
            return [
                {"tool": c.tool_name, "error": c.error, "time": c.timestamp.isoformat()}
                for c in errors[-n:]
            ]

    def get_recent_calls(self, n: int = 10) -> list[dict]:
        with self._lock:
            return [
                {
                    "tool": c.tool_name,
                    "time": c.timestamp.isoformat(),
                    "latency_ms": round(c.latency_ms, 2),
                    "success": c.success,
                    "error": c.error,
                }
                for c in reversed(self._calls[-n:])
            ]


_sheets_logger = SheetsLogger()


def log_tool_call(func: Callable) -> Callable:
    """Decorator — log every tool call with latency and success/failure."""
    def wrapper(*args, **kwargs):
        start = time.time()
        preview = ", ".join(str(a)[:50] for a in args if a is not None)
        try:
            result = func(*args, **kwargs)
            _sheets_logger.log(func.__name__, (time.time() - start) * 1000,
                               success=True, args_preview=preview)
            return result
        except Exception as exc:
            _sheets_logger.log(func.__name__, (time.time() - start) * 1000,
                               success=False, error=str(exc)[:100], args_preview=preview)
            raise
    return wrapper


# ═════════════════════════════════════════════════════════════════════════════
# CONTEXT — tracks active sheet for natural language resolution
# ═════════════════════════════════════════════════════════════════════════════

class SheetContext:
    """Track spreadsheet references for natural language resolution."""

    def __init__(self):
        self.current_sheet_id: Optional[str] = None
        self.current_tab: Optional[str] = None
        self.last_range: Optional[str] = None
        self.last_read_headers: list[str] = []
        self.last_viewed_ids: list[str] = []
        self.last_search_results: list[dict] = []
        self._lock = threading.Lock()

    def add_viewed(self, sheet_id: str, tab: str = None) -> None:
        with self._lock:
            if sheet_id in self.last_viewed_ids:
                self.last_viewed_ids.remove(sheet_id)
            self.last_viewed_ids.insert(0, sheet_id)
            self.last_viewed_ids = self.last_viewed_ids[:20]
            self.current_sheet_id = sheet_id
            if tab:
                self.current_tab = tab

    def add_search_results(self, sheets: list[dict]) -> None:
        with self._lock:
            self.last_search_results = sheets[:10]

    def set_last_range(self, range_name: str, headers: list[str] = None) -> None:
        with self._lock:
            self.last_range = range_name
            if headers is not None:
                self.last_read_headers = headers

    def resolve_reference(self, reference: str) -> Optional[str]:
        """Resolve natural language → sheet ID from context."""
        ref = reference.lower().strip()
        with self._lock:
            if ref in ("this sheet", "this spreadsheet", "that sheet",
                       "current", "it", "current sheet"):
                return self.current_sheet_id

            if ref in ("latest", "last", "most recent", "recent") and self.last_viewed_ids:
                return self.last_viewed_ids[0]

            ordinals = {
                "first": 0, "1st": 0, "second": 1, "2nd": 1,
                "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
                "fifth": 4, "5th": 4,
            }
            if ref in ordinals:
                idx = ordinals[ref]
                if idx < len(self.last_search_results):
                    return self.last_search_results[idx].get("id")

            return None

    def get_all_recent_ids(self) -> list[str]:
        with self._lock:
            return [s.get("id", "") for s in self.last_search_results if s.get("id")]

    def get_context_summary(self) -> dict:
        with self._lock:
            current = None
            if self.current_sheet_id:
                for s in self.last_search_results:
                    if s.get("id") == self.current_sheet_id:
                        current = {
                            "id": s["id"],
                            "title": s.get("title", ""),
                            "modified": s.get("modified", ""),
                        }
                        break
                if not current:
                    current = {"id": self.current_sheet_id, "title": "", "modified": ""}

            recent = [
                {
                    "index": i + 1,
                    "id": s.get("id", ""),
                    "title": s.get("title", "Untitled"),
                    "modified": s.get("modified", ""),
                }
                for i, s in enumerate(self.last_search_results[:5])
            ]

            return {
                "current_sheet": current,
                "current_tab": self.current_tab,
                "last_range": self.last_range,
                "last_headers": self.last_read_headers,
                "recent_sheets": recent,
                "total_tracked": len(self.last_viewed_ids),
            }


_sheet_context = SheetContext()


# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

def validate_sheet_title(title: str) -> tuple[bool, str]:
    if not title or not title.strip():
        return False, "Title cannot be empty"
    if len(title) > 100:
        return False, "Title too long (max 100 characters)"
    return True, ""


def sanitize_search_query(query: str) -> str:
    """Strip characters that would break Drive API query strings."""
    return re.sub(r"['\\\"]", " ", query).strip()


def extract_sheet_id_from_url(url: str) -> Optional[str]:
    """Extract spreadsheet ID from a Google Sheets URL."""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9\-_]+)", url)
    return m.group(1) if m else None


# ═════════════════════════════════════════════════════════════════════════════
# COLUMN / RANGE UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def col_letter_to_index(col: str) -> int:
    """Convert column letter to 0-based index. "A"→0, "Z"→25, "AA"→26."""
    col = col.strip().upper()
    result = 0
    for ch in col:
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


def col_index_to_letter(idx: int) -> str:
    """Convert 0-based column index to letter. 0→"A", 25→"Z", 26→"AA"."""
    result = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


# Matches standalone A1 notation (with optional sheet prefix)
_A1_RE = re.compile(
    r"^(?:[A-Za-z0-9 _]+!)?[A-Za-z]{1,3}\d*(?::[A-Za-z]{1,3}\d*)?$"
)
_ROW_RANGE_RE = re.compile(r"^\d+:\d+$")


def parse_natural_range(
    reference: str,
    headers: list[str] = None,
    row_count: int = None,
    col_count: int = None,
) -> str:
    """
    Convert a natural language range description to A1 notation.

    Handles:
      - A1 notation passthrough        "A1:C10", "Sheet1!A:D"
      - Row ranges                     "1:5"
      - "first/top N rows"             → "1:<N+1>" (includes header)
      - "last N rows"                  → "<start>:<end>"
      - "row N"                        → "N:N"
      - "rows N to M"                  → "N:M"
      - "column X" (letter)            → "X:X"
      - "last column"                  → resolved from col_count
      - "last row"                     → resolved from row_count
      - Named column lookup in headers → resolved from headers list
      - "all" / "everything"           → "A:ZZ"
    """
    ref = reference.strip()

    if _A1_RE.match(ref) or _ROW_RANGE_RE.match(ref):
        return ref

    rl = ref.lower()

    # "first/top N rows"
    m = re.match(r"(?:first|top)\s+(\d+)\s+rows?", rl)
    if m:
        n = int(m.group(1))
        return f"1:{n + 1}"

    # "last N rows"
    m = re.match(r"last\s+(\d+)\s+rows?", rl)
    if m:
        n = int(m.group(1))
        if row_count:
            start = max(1, row_count - n + 1)
            return f"{start}:{row_count}"
        return f"A1:ZZ{n}"

    # "rows N to M"
    m = re.match(r"rows?\s+(\d+)\s+(?:to|through|-)\s+(\d+)", rl)
    if m:
        return f"{m.group(1)}:{m.group(2)}"

    # "row N"
    m = re.match(r"rows?\s+(\d+)$", rl)
    if m:
        n = m.group(1)
        return f"{n}:{n}"

    # "column X" (letter)
    m = re.match(r"col(?:umn)?\s+([A-Za-z]+)$", rl)
    if m:
        return f"{m.group(1).upper()}:{m.group(1).upper()}"

    # "last column"
    if rl in ("last column", "last col", "final column"):
        if col_count:
            letter = col_index_to_letter(col_count - 1)
            return f"{letter}:{letter}"
        return "A:A"

    # "last row"
    if rl in ("last row", "bottom row", "final row"):
        if row_count:
            return f"{row_count}:{row_count}"
        return "A1:ZZ1"

    # named column — exact then partial header match
    if headers:
        for i, h in enumerate(headers):
            if h and rl == str(h).strip().lower():
                letter = col_index_to_letter(i)
                return f"{letter}:{letter}"
        for i, h in enumerate(headers):
            if h and rl in str(h).strip().lower():
                letter = col_index_to_letter(i)
                return f"{letter}:{letter}"

    # "all" / "everything"
    if rl in ("all", "all data", "everything", "whole sheet", "entire sheet"):
        return "A:ZZ"

    return ref


# ═════════════════════════════════════════════════════════════════════════════
# TABLE INTELLIGENCE
# ═════════════════════════════════════════════════════════════════════════════

def _is_numeric(val: Any) -> bool:
    if not val and val != 0:
        return False
    try:
        float(str(val).replace(",", "").replace("%", "").replace("$", "").strip())
        return True
    except (ValueError, TypeError):
        return False


def detect_headers(rows: list) -> list[str]:
    """
    Detect if the first row is a header row.
    Returns list of header strings if detected, else empty list.
    """
    if not rows or not rows[0]:
        return []
    first = rows[0]
    non_numeric = sum(1 for cell in first if not _is_numeric(cell))
    if first and non_numeric / len(first) >= 0.7:
        return [str(cell).strip() for cell in first]
    return []


def get_column_index(headers: list[str], column_name: str) -> Optional[int]:
    """
    Find 0-based column index by name. Exact match first, then partial.
    Returns None if not found.
    """
    col_lower = column_name.lower().strip()
    for i, h in enumerate(headers):
        if h and h.lower().strip() == col_lower:
            return i
    for i, h in enumerate(headers):
        if h and col_lower in h.lower():
            return i
    return None


def compute_column_stats(values: list) -> dict:
    """Compute statistics for a column of values (numeric or text)."""
    numeric: list[float] = []
    for v in values:
        if _is_numeric(v):
            try:
                numeric.append(float(str(v).replace(",", "").replace("%", "").replace("$", "").strip()))
            except ValueError:
                pass

    non_empty = [v for v in values if v is not None and str(v).strip() != ""]

    if numeric:
        result: dict = {
            "type": "numeric",
            "count": len(values),
            "non_empty": len(non_empty),
            "empty": len(values) - len(non_empty),
            "min": min(numeric),
            "max": max(numeric),
            "sum": round(sum(numeric), 6),
            "mean": round(statistics.mean(numeric), 6),
            "median": round(statistics.median(numeric), 6),
        }
        if len(numeric) > 1:
            result["stdev"] = round(statistics.stdev(numeric), 6)
        return result

    unique_vals = list({str(v).strip() for v in non_empty})
    return {
        "type": "text",
        "count": len(values),
        "non_empty": len(non_empty),
        "empty": len(values) - len(non_empty),
        "unique_count": len(unique_vals),
        "sample_values": unique_vals[:10],
    }


def detect_outliers_in_column(values: list) -> list[dict]:
    """
    Detect numeric outliers using the IQR method.
    Returns list of {row, value, reason} dicts.
    """
    pairs: list[tuple[int, float]] = []
    for i, v in enumerate(values):
        if _is_numeric(v):
            try:
                pairs.append((i, float(str(v).replace(",", "").replace("$", "").strip())))
            except ValueError:
                pass

    if len(pairs) < 4:
        return []

    sorted_vals = sorted(p[1] for p in pairs)
    n = len(sorted_vals)
    q1 = sorted_vals[n // 4]
    q3 = sorted_vals[3 * n // 4]
    iqr = q3 - q1
    lower = q1 - 1.5 * iqr
    upper = q3 + 1.5 * iqr

    return [
        {
            "row": idx + 1,
            "value": val,
            "reason": "below lower bound" if val < lower else "above upper bound",
        }
        for idx, val in pairs
        if val < lower or val > upper
    ]


def detect_data_range(rows: list) -> tuple[int, int, int, int]:
    """
    Find bounding box of non-empty data.
    Returns (first_data_row, last_data_row, first_col, last_col) — 0-based.
    """
    if not rows:
        return 0, 0, 0, 0

    first_row = 0
    last_row = len(rows) - 1
    first_col = 0
    last_col = 0

    for r, row in enumerate(rows):
        if any(cell and str(cell).strip() for cell in row):
            first_row = r
            break

    for r in range(len(rows) - 1, -1, -1):
        if any(cell and str(cell).strip() for cell in rows[r]):
            last_row = r
            break

    all_cols = max((len(row) for row in rows), default=0)
    last_col = all_cols - 1

    return first_row, last_row, first_col, last_col


# ═════════════════════════════════════════════════════════════════════════════
# SAFETY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def check_write_safety(
    new_row_count: int,
    existing_row_count: int = 0,
    require_confirmation: bool = False,
) -> tuple[bool, str]:
    """
    Returns (needs_confirmation, warning_message).
    needs_confirmation=True → caller should raise SheetSafetyError.
    """
    warnings: list[str] = []

    if new_row_count > LARGE_DATA_THRESHOLD:
        warnings.append(
            f"Writing {new_row_count} rows is a large operation."
        )
    if existing_row_count > LARGE_DATA_THRESHOLD:
        warnings.append(
            f"Overwriting a range with {existing_row_count} existing rows is irreversible."
        )

    if not warnings:
        return False, ""

    msg = "; ".join(warnings)
    if require_confirmation:
        return True, msg
    return False, msg


def check_delete_safety(
    row_count: int,
    require_confirmation: bool = True,
) -> tuple[bool, str]:
    """Safety gate for bulk row deletion."""
    if row_count > BATCH_ROW_LIMIT:
        return True, f"Cannot delete more than {BATCH_ROW_LIMIT} rows at once (requested {row_count})"
    if require_confirmation and row_count > 10:
        return True, (
            f"Deleting {row_count} rows is irreversible. "
            "Set require_confirmation=False to proceed."
        )
    return False, ""


_DANGEROUS_FORMULA_RE = re.compile(
    r"=\s*IMPORTXML\s*\(|=\s*WEBSERVICE\s*\(|=\s*INDIRECT\s*\(",
    re.IGNORECASE,
)


def check_formula_safety(formula: str) -> tuple[bool, str]:
    """
    Validate formula. Returns (is_valid, warning_or_error).
    is_valid=False → caller should raise SheetValidationError.
    """
    if not formula or not formula.strip().startswith("="):
        return False, "Formula must start with '='"
    if _DANGEROUS_FORMULA_RE.search(formula):
        return False, "Formula contains a potentially unsafe function (IMPORTXML, WEBSERVICE, INDIRECT)"
    if len(formula) > 2000:
        return False, "Formula is too long (max 2000 characters)"
    return True, ""


# ═════════════════════════════════════════════════════════════════════════════
# RESPONSE STANDARDIZATION
# ═════════════════════════════════════════════════════════════════════════════

def standardize_sheet_response(sheet: dict) -> dict:
    """Normalize a Drive/Sheets file record for consistent orchestrator output."""
    sid = sheet.get("spreadsheetId", sheet.get("id", ""))
    return {
        "id": sid,
        "title": sheet.get("title", sheet.get("name", "Untitled")),
        "url": sheet.get("webViewLink", f"https://docs.google.com/spreadsheets/d/{sid}/edit"),
        "created": sheet.get("createdTime", ""),
        "modified": sheet.get("modifiedTime", ""),
        "owner": (sheet.get("owners") or [{}])[0].get("displayName", "Unknown"),
    }


def paginated_response(
    items: list,
    next_page_token: str = "",
    has_more: bool = False,
    total_count: int = None,
) -> dict:
    return {
        "items": items,
        "next_page_token": next_page_token,
        "has_more": has_more,
        "returned_count": len(items),
        "total_count": total_count if total_count is not None else len(items),
    }


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Errors
    "SheetError", "SheetNotFoundError", "SheetAmbiguityError", "SheetPermissionError",
    "SheetRateLimitError", "SheetValidationError", "SheetRangeError", "SheetSafetyError",
    # Cache
    "SimpleCache", "_sheets_cache", "cached", "invalidate_cache",
    # Constants
    "MAX_ROWS_PER_READ", "LARGE_DATA_THRESHOLD", "BATCH_ROW_LIMIT", "BATCH_RANGE_LIMIT",
    # Logging
    "SheetsLogger", "_sheets_logger", "log_tool_call",
    # Context
    "SheetContext", "_sheet_context",
    # Validation
    "validate_sheet_title", "sanitize_search_query", "extract_sheet_id_from_url",
    # Range utilities
    "col_letter_to_index", "col_index_to_letter", "parse_natural_range",
    # Table intelligence
    "detect_headers", "get_column_index", "compute_column_stats",
    "detect_outliers_in_column", "detect_data_range",
    # Safety
    "check_write_safety", "check_delete_safety", "check_formula_safety",
    # Response
    "standardize_sheet_response", "paginated_response",
]
