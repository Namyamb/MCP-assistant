"""
Google Docs MCP v2 — Infrastructure Layer

Provides: errors, caching, logging, validation, context management, safety helpers
"""

from __future__ import annotations

import time
import re
import threading
from typing import Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime

# ═════════════════════════════════════════════════════════════════════════════
# CUSTOM ERROR CLASSES
# ═════════════════════════════════════════════════════════════════════════════

class DocError(Exception):
    """Base exception for Docs MCP."""
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class DocNotFoundError(DocError):
    """Document not found."""
    pass


class DocAmbiguityError(DocError):
    """Multiple documents match the reference."""
    def __init__(self, message: str, matches: list = None):
        super().__init__(message)
        self.matches = matches or []


class DocPermissionError(DocError):
    """Permission denied."""
    pass


class DocRateLimitError(DocError):
    """API rate limit hit."""
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message)
        self.retry_after = retry_after


class DocValidationError(DocError):
    """Invalid arguments."""
    def __init__(self, param: str, value: Any, reason: str):
        super().__init__(f"Invalid value for '{param}': {value} — {reason}")
        self.param = param
        self.value = value
        self.reason = reason


class DocSafetyError(DocError):
    """Safety check failed (destructive operation)."""
    pass


# ═════════════════════════════════════════════════════════════════════════════
# LIGHTWEIGHT TTL CACHE
# ═════════════════════════════════════════════════════════════════════════════

class SimpleCache:
    """Thread-safe TTL cache for document metadata and content."""
    
    def __init__(self, default_ttl: int = 300):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._lock = threading.RLock()
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None
            value, expiry = self._cache[key]
            if time.time() > expiry:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return value
    
    def set(self, key: str, value: Any, ttl: int = None) -> None:
        with self._lock:
            ttl = ttl or self._default_ttl
            self._cache[key] = (value, time.time() + ttl)
    
    def invalidate(self, prefix: str = "") -> int:
        with self._lock:
            if not prefix:
                count = len(self._cache)
                self._cache.clear()
                return count
            to_delete = [k for k in self._cache if k.startswith(prefix)]
            for k in to_delete:
                del self._cache[k]
            return len(to_delete)
    
    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0
            return {
                "size": len(self._cache),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(hit_rate, 2)
            }


# Global cache instance
_docs_cache = SimpleCache(default_ttl=300)

# Safety thresholds
LARGE_CONTENT_THRESHOLD = 5000   # chars — warn when replacing/inserting more than this
LARGE_SECTION_THRESHOLD = 2000   # chars — warn when overwriting a section larger than this
BATCH_SECTION_LIMIT     = 20     # max sections per batch append
BATCH_REPLACE_LIMIT     = 10     # max sections per batch replace/delete


def cached(namespace: str, ttl: int = 300):
    """Decorator to cache function results."""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            # Build cache key from function name and arguments
            key_parts = [namespace, func.__name__]
            key_parts.extend(str(a) for a in args)
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)
            
            cached_value = _docs_cache.get(cache_key)
            if cached_value is not None:
                return cached_value
            
            result = func(*args, **kwargs)
            _docs_cache.set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator


def invalidate_cache(prefix: str = "") -> dict:
    """Invalidate cache entries by prefix."""
    count = _docs_cache.invalidate(prefix)
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


class DocsLogger:
    """Structured logging for Docs MCP operations."""
    
    def __init__(self, max_history: int = 100):
        self._calls: list[ToolCallLog] = []
        self._lock = threading.Lock()
        self._max_history = max_history
    
    def log(self, tool_name: str, latency_ms: float, success: bool, 
            error: str = None, args_preview: str = ""):
        with self._lock:
            log_entry = ToolCallLog(
                tool_name=tool_name,
                timestamp=datetime.now(),
                latency_ms=latency_ms,
                success=success,
                error=error,
                args_preview=args_preview[:200]  # Truncate long args
            )
            self._calls.append(log_entry)
            if len(self._calls) > self._max_history:
                self._calls = self._calls[-self._max_history:]
    
    def get_recent_calls(self, n: int = 10) -> list[dict]:
        with self._lock:
            recent = self._calls[-n:]
            return [
                {
                    "tool": c.tool_name,
                    "time": c.timestamp.isoformat(),
                    "latency_ms": round(c.latency_ms, 2),
                    "success": c.success,
                    "error": c.error,
                    "args": c.args_preview
                }
                for c in reversed(recent)
            ]
    
    def get_stats(self) -> dict:
        with self._lock:
            if not self._calls:
                return {"total_calls": 0}

            total = len(self._calls)
            successful = sum(1 for c in self._calls if c.success)
            avg_latency = sum(c.latency_ms for c in self._calls) / total

            tool_counts = {}
            for c in self._calls:
                tool_counts[c.tool_name] = tool_counts.get(c.tool_name, 0) + 1

            return {
                "total_calls": total,
                "success_rate": round(successful / total, 2),
                "avg_latency_ms": round(avg_latency, 2),
                "tool_breakdown": tool_counts
            }

    def get_tool_stats(self) -> dict:
        """Per-tool success rate and average latency."""
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
        """Return the most recent failed calls."""
        with self._lock:
            errors = [c for c in self._calls if not c.success]
            return [
                {"tool": c.tool_name, "error": c.error, "time": c.timestamp.isoformat()}
                for c in errors[-n:]
            ]


# Global logger instance
_docs_logger = DocsLogger()


def log_tool_call(func: Callable) -> Callable:
    """Decorator to log tool calls with latency."""
    def wrapper(*args, **kwargs):
        start = time.time()
        tool_name = func.__name__
        
        # Build args preview (sanitized)
        args_preview = ", ".join(str(a)[:50] for a in args if a is not None)
        
        try:
            result = func(*args, **kwargs)
            latency = (time.time() - start) * 1000
            _docs_logger.log(tool_name, latency, success=True, args_preview=args_preview)
            return result
        except Exception as e:
            latency = (time.time() - start) * 1000
            _docs_logger.log(tool_name, latency, success=False, 
                           error=str(e)[:100], args_preview=args_preview)
            raise
    return wrapper


# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def validate_doc_title(title: str) -> tuple[bool, str]:
    """Validate document title."""
    if not title:
        return False, "Title cannot be empty"
    if len(title) > 100:
        return False, "Title too long (max 100 characters)"
    if re.search(r'[<>:"/\\|?*]', title):
        return False, "Title contains invalid characters"
    return True, ""


def sanitize_search_query(query: str) -> str:
    """Sanitize search query for Drive API."""
    # Remove special characters that could break search
    return re.sub(r'[^\w\s\-\'\.]', ' ', query).strip()


def extract_doc_id_from_url(url: str) -> Optional[str]:
    """Extract document ID from Google Docs URL."""
    patterns = [
        r'/document/d/([a-zA-Z0-9\-_]+)',
        r'/spreadsheets/d/([a-zA-Z0-9\-_]+)',
        r'id=([a-zA-Z0-9\-_]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ═════════════════════════════════════════════════════════════════════════════
# RESPONSE STANDARDIZATION
# ═════════════════════════════════════════════════════════════════════════════

def standardize_doc_response(doc: dict, content_preview: str = "") -> dict:
    """Standardize document metadata for orchestrator context."""
    return {
        "id": doc.get("documentId", doc.get("id", "")),
        "title": doc.get("title", "Untitled"),
        "url": f"https://docs.google.com/document/d/{doc.get('documentId', doc.get('id', ''))}/edit",
        "created": doc.get("createdTime", ""),
        "modified": doc.get("modifiedTime", ""),
        "owner": doc.get("owners", [{}])[0].get("displayName", "Unknown") if doc.get("owners") else "Unknown",
        "content_preview": content_preview[:200] if content_preview else "",
    }


def paginated_response(items: list, next_page_token: str = "", 
                      has_more: bool = False, total_count: int = None) -> dict:
    """Standard paginated response format."""
    return {
        "items": items,
        "next_page_token": next_page_token,
        "has_more": has_more,
        "returned_count": len(items),
        "total_count": total_count or len(items)
    }


# ═════════════════════════════════════════════════════════════════════════════
# CONTEXT / MEMORY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

class DocContext:
    """Track document references for natural language resolution."""
    
    def __init__(self):
        self.last_viewed_ids: list[str] = []
        self.last_search_results: list[dict] = []
        self.current_doc_id: Optional[str] = None
        self._lock = threading.Lock()
    
    def add_viewed(self, doc_id: str) -> None:
        """Track a viewed document."""
        with self._lock:
            if doc_id in self.last_viewed_ids:
                self.last_viewed_ids.remove(doc_id)
            self.last_viewed_ids.insert(0, doc_id)
            self.last_viewed_ids = self.last_viewed_ids[:20]  # Keep last 20
            self.current_doc_id = doc_id
    
    def add_search_results(self, docs: list[dict]) -> None:
        """Store search results for ordinal references."""
        with self._lock:
            self.last_search_results = docs[:10]  # Keep top 10
    
    def set_current_doc(self, doc_id: str) -> None:
        """Set current document being edited."""
        with self._lock:
            self.current_doc_id = doc_id
    
    def clear_current_doc(self) -> None:
        """Clear current document."""
        with self._lock:
            self.current_doc_id = None
    
    def resolve_reference(self, reference: str) -> Optional[str]:
        """Resolve natural language reference to document ID."""
        ref = reference.lower().strip()

        with self._lock:
            if ref in ("this document", "this doc", "that document", "that doc", "it", "current"):
                return self.current_doc_id

            if ref in ("latest", "last", "most recent", "recent") and self.last_viewed_ids:
                return self.last_viewed_ids[0]

            ordinals = {
                "first": 0, "1st": 0, "1": 0,
                "second": 1, "2nd": 1, "2": 1,
                "third": 2, "3rd": 2, "3": 2,
                "fourth": 3, "4th": 3, "4": 3,
                "fifth": 4, "5th": 4, "5": 4,
            }
            if ref in ordinals:
                idx = ordinals[ref]
                if idx < len(self.last_search_results):
                    return self.last_search_results[idx].get("id")

            return None

    def get_all_recent_ids(self) -> list[str]:
        """Return all IDs from the last search — for 'edit all these docs' type requests."""
        with self._lock:
            return [d.get("id", "") for d in self.last_search_results if d.get("id")]

    def get_context_summary(self) -> dict:
        """Structured context snapshot for LLM system-prompt injection."""
        with self._lock:
            current = None
            if self.current_doc_id:
                for d in self.last_search_results:
                    if d.get("id") == self.current_doc_id:
                        current = {"id": d["id"], "title": d.get("title", ""), "modified": d.get("modified", "")}
                        break
                if not current:
                    current = {"id": self.current_doc_id, "title": "", "modified": ""}

            recent = [
                {
                    "index": i + 1,
                    "id": d.get("id", ""),
                    "title": d.get("title", "Untitled"),
                    "modified": d.get("modified", ""),
                }
                for i, d in enumerate(self.last_search_results[:5])
            ]

            return {
                "current_document": current,
                "recent_documents": recent,
                "total_tracked": len(self.last_viewed_ids),
            }


# Global context instance
_doc_context = DocContext()


# ═════════════════════════════════════════════════════════════════════════════
# SAFETY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def check_content_safety(content: str, action: str) -> tuple[bool, str]:
    """Check if content operation is safe."""
    warnings = []

    if len(content) > LARGE_CONTENT_THRESHOLD:
        warnings.append(f"Large content ({len(content)} chars) — operation may be slow")

    destructive_actions = ["delete", "replace", "clear"]
    if action in destructive_actions:
        warnings.append(f"Destructive action '{action}' — content will be modified")

    is_safe = len(warnings) == 0
    return is_safe, "; ".join(warnings) if warnings else ""


def check_replace_safety(
    new_content: str,
    existing_section_size: int = 0,
    require_confirmation: bool = False
) -> tuple[bool, str]:
    """
    Safety check specifically for section replacement.
    Returns (needs_confirmation, warning_message).
    """
    warnings = []

    if len(new_content) > LARGE_CONTENT_THRESHOLD:
        warnings.append(
            f"Replacement content is large ({len(new_content)} chars). "
            "This will significantly change the document."
        )
    if existing_section_size > LARGE_SECTION_THRESHOLD:
        warnings.append(
            f"Overwriting a large section ({existing_section_size} chars). "
            "This is irreversible."
        )

    if not warnings:
        return False, ""

    warning_msg = "; ".join(warnings)
    if require_confirmation:
        return True, warning_msg   # caller should raise DocSafetyError
    return False, warning_msg      # warn but allow


def check_delete_batch_safety(section_count: int, require_confirmation: bool = True) -> tuple[bool, str]:
    """Safety check for deleting multiple sections."""
    if section_count > BATCH_REPLACE_LIMIT:
        return True, f"Cannot delete more than {BATCH_REPLACE_LIMIT} sections at once (requested {section_count})"
    if require_confirmation and section_count > 1:
        return True, (
            f"Deleting {section_count} sections is irreversible. "
            "Set require_confirmation=False to proceed."
        )
    return False, ""


def check_batch_safety(doc_ids: list[str], action: str) -> tuple[bool, str]:
    """Check if batch operation is safe."""
    if len(doc_ids) > 50:
        return False, f"Cannot {action} more than 50 documents at once"
    if len(doc_ids) > 10:
        return True, f"Warning: Large batch {action} on {len(doc_ids)} documents"
    return True, ""


# ═════════════════════════════════════════════════════════════════════════════
# CONTENT INTELLIGENCE
# ═════════════════════════════════════════════════════════════════════════════

def extract_sections(content: str) -> list[dict]:
    """Extract sections (headings) from document content."""
    sections = []
    lines = content.split('\n')
    
    for i, line in enumerate(lines):
        # Check for headings (simple heuristic)
        if line.startswith('# ') or line.startswith('## ') or line.startswith('### '):
            level = line.count('#', 0, 4)
            title = line.strip('# ').strip()
            sections.append({
                "index": i,
                "level": level,
                "title": title,
                "type": "heading"
            })
        # Check for bullet points
        elif line.strip().startswith(('- ', '* ', '• ')):
            sections.append({
                "index": i,
                "level": 0,
                "title": line.strip('- *•').strip()[:50],
                "type": "bullet"
            })
    
    return sections


def get_content_type_intelligence(content: str) -> dict:
    """Analyze content type and provide suggestions."""
    lines = content.split('\n')
    
    has_tables = '|' in content and content.count('|') > 2
    has_headings = any(l.startswith('#') for l in lines)
    has_bullets = any(l.strip().startswith(('- ', '* ')) for l in lines)
    has_numbers = any(l.strip()[0].isdigit() for l in lines if l.strip())
    
    doc_type = "document"
    if has_tables and has_numbers:
        doc_type = "spreadsheet-like"
    elif has_headings and has_bullets:
        doc_type = "structured-notes"
    elif has_bullets:
        doc_type = "list"
    elif has_headings:
        doc_type = "structured-document"
    
    return {
        "detected_type": doc_type,
        "has_tables": has_tables,
        "has_headings": has_headings,
        "has_lists": has_bullets,
        "line_count": len(lines),
        "char_count": len(content)
    }


# ═════════════════════════════════════════════════════════════════════════════
# DOCUMENT STRUCTURE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def find_section_boundaries(content: str, section_title: str) -> tuple[int, int]:
    """Find start and end positions of a section by heading."""
    lines = content.split('\n')
    start_idx = -1
    end_idx = len(lines)
    
    for i, line in enumerate(lines):
        # Check if this line is our target heading
        if section_title.lower() in line.lower():
            if line.startswith('#') or line.strip().lower() == section_title.lower():
                start_idx = i
                # Find next heading of same or higher level
                current_level = line.count('#', 0, 4) if line.startswith('#') else 0
                for j in range(i + 1, len(lines)):
                    next_line = lines[j]
                    if next_line.startswith('#' * (current_level + 1)):
                        end_idx = j
                        break
                break
    
    return start_idx, end_idx


def truncate_content(content: str, limit: int = 1000) -> tuple[str, bool]:
    """Truncate content for preview."""
    if len(content) <= limit:
        return content, False
    return content[:limit] + "\n\n[... content truncated ...]", True


__all__ = [
    # Errors
    "DocError", "DocNotFoundError", "DocAmbiguityError", "DocPermissionError",
    "DocRateLimitError", "DocValidationError", "DocSafetyError",
    # Cache
    "SimpleCache", "cached", "invalidate_cache", "_docs_cache",
    # Thresholds
    "LARGE_CONTENT_THRESHOLD", "LARGE_SECTION_THRESHOLD",
    "BATCH_SECTION_LIMIT", "BATCH_REPLACE_LIMIT",
    # Logging
    "DocsLogger", "log_tool_call", "_docs_logger",
    # Validation
    "validate_doc_title", "sanitize_search_query", "extract_doc_id_from_url",
    # Response
    "standardize_doc_response", "paginated_response",
    # Context
    "DocContext", "_doc_context",
    # Safety
    "check_content_safety", "check_batch_safety",
    "check_replace_safety", "check_delete_batch_safety",
    # Intelligence
    "extract_sections", "get_content_type_intelligence", "find_section_boundaries", "truncate_content",
]
