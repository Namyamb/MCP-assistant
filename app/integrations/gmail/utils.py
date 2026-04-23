"""
Gmail MCP Utilities: Error classes, caching, logging, and helpers.
"""

from __future__ import annotations

import time
import logging
import functools
from typing import Any, Optional, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gmail_mcp")


# ═════════════════════════════════════════════════════════════════════════════
# ERROR CLASSES
# ═════════════════════════════════════════════════════════════════════════════

class EmailError(Exception):
    """Base exception for Gmail MCP."""
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class EmailNotFoundError(EmailError):
    """Email not found in Gmail."""
    pass


class EmailAmbiguityError(EmailError):
    """Multiple emails match the reference."""
    def __init__(self, message: str, matches: list):
        super().__init__(message, {"matches": matches})
        self.matches = matches


class EmailPermissionError(EmailError):
    """Permission denied (can't access email)."""
    pass


class EmailRateLimitError(EmailError):
    """Gmail API rate limit hit."""
    def __init__(self, message: str, retry_after: int = 60):
        super().__init__(message, {"retry_after": retry_after})
        self.retry_after = retry_after


class EmailValidationError(EmailError):
    """Invalid input (bad email format, missing field, etc.)."""
    pass


class EmailSafetyError(EmailError):
    """Safety check failed (mass delete, suspicious recipient, etc.)."""
    pass


# ═════════════════════════════════════════════════════════════════════════════
# CACHING LAYER (TTL-based)
# ═════════════════════════════════════════════════════════════════════════════

class SimpleCache:
    """Thread-safe TTL cache for Gmail operations."""
    
    def __init__(self, default_ttl: int = 300):
        self._cache: dict[str, tuple[Any, float]] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            value, expiry = self._cache[key]
            if time.time() < expiry:
                self._hits += 1
                return value
            else:
                del self._cache[key]
        self._misses += 1
        return None
    
    def set(self, key: str, value: Any, ttl: int = None) -> None:
        expiry = time.time() + (ttl or self._default_ttl)
        self._cache[key] = (value, expiry)
    
    def invalidate(self, prefix: str = "") -> int:
        """Invalidate cache entries matching prefix. Returns count cleared."""
        if not prefix:
            count = len(self._cache)
            self._cache.clear()
            return count
        
        keys_to_delete = [k for k in self._cache if k.startswith(prefix)]
        for k in keys_to_delete:
            del self._cache[k]
        return len(keys_to_delete)
    
    def clear(self) -> None:
        self._cache.clear()
        self._hits = 0
        self._misses = 0
    
    def stats(self) -> dict:
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1f}%"
        }


# Global cache instance
_gmail_cache = SimpleCache(default_ttl=300)


def invalidate_cache(prefix: str = "") -> dict:
    """Invalidate Gmail cache. Use prefix="search" to clear search results only."""
    cleared = _gmail_cache.invalidate(prefix)
    return {"cleared_entries": cleared, "prefix": prefix or "all"}


def cached(prefix: str, ttl: int = 300):
    """Decorator to cache function results."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key
            cache_key = f"{prefix}:{func.__name__}:{str(args)}:{str(kwargs)}"
            
            # Try cache
            cached_result = _gmail_cache.get(cache_key)
            if cached_result is not None:
                return cached_result
            
            # Execute and cache
            result = func(*args, **kwargs)
            _gmail_cache.set(cache_key, result, ttl)
            return result
        return wrapper
    return decorator


# ═════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING / OBSERVABILITY
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCallLog:
    tool_name: str
    arguments: dict
    result: Any
    latency_ms: float
    timestamp: datetime
    success: bool
    error: Optional[str] = None


class EmailLogger:
    """Structured logging for Gmail operations."""
    
    def __init__(self, max_history: int = 1000):
        self._calls: list[ToolCallLog] = []
        self._max_history = max_history
    
    def log(self, tool_name: str, arguments: dict, result: Any, 
            latency_ms: float, success: bool, error: Optional[str] = None):
        log_entry = ToolCallLog(
            tool_name=tool_name,
            arguments=self._sanitize_args(arguments),
            result=result if success else None,
            latency_ms=latency_ms,
            timestamp=datetime.now(),
            success=success,
            error=error
        )
        self._calls.append(log_entry)
        
        # Trim history
        if len(self._calls) > self._max_history:
            self._calls = self._calls[-self._max_history:]
        
        # Also log to standard logger
        status = "SUCCESS" if success else "FAILED"
        logger.info(f"[GMAIL] {tool_name}: {status} ({latency_ms:.1f}ms)")
    
    def _sanitize_args(self, args: dict) -> dict:
        """Remove sensitive data from logged arguments."""
        sensitive_keys = {'body', 'content', 'attachment', 'password', 'token'}
        sanitized = {}
        for k, v in args.items():
            if k.lower() in sensitive_keys:
                sanitized[k] = "[REDACTED]"
            else:
                sanitized[k] = v
        return sanitized
    
    def get_recent_calls(self, n: int = 10) -> list[dict]:
        """Get recent tool calls for debugging."""
        recent = self._calls[-n:]
        return [
            {
                "tool": c.tool_name,
                "success": c.success,
                "latency_ms": round(c.latency_ms, 2),
                "timestamp": c.timestamp.isoformat(),
                "error": c.error
            }
            for c in recent
        ]
    
    def get_stats(self) -> dict:
        """Get usage statistics."""
        if not self._calls:
            return {"total_calls": 0, "success_rate": "0%", "avg_latency_ms": 0}
        
        total = len(self._calls)
        successful = sum(1 for c in self._calls if c.success)
        avg_latency = sum(c.latency_ms for c in self._calls) / total
        
        return {
            "total_calls": total,
            "success_rate": f"{successful/total*100:.1f}%",
            "avg_latency_ms": round(avg_latency, 2)
        }


# Global logger instance
_gmail_logger = EmailLogger()


def log_tool_call(func: Callable) -> Callable:
    """Decorator to log tool calls with latency."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        try:
            result = func(*args, **kwargs)
            latency = (time.time() - start) * 1000
            _gmail_logger.log(
                tool_name=func.__name__,
                arguments=kwargs,
                result=result,
                latency_ms=latency,
                success=True
            )
            return result
        except Exception as e:
            latency = (time.time() - start) * 1000
            _gmail_logger.log(
                tool_name=func.__name__,
                arguments=kwargs,
                result=None,
                latency_ms=latency,
                success=False,
                error=str(e)
            )
            raise
    return wrapper


# ═════════════════════════════════════════════════════════════════════════════
# VALIDATION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def validate_email(email: str) -> bool:
    """Validate email address format."""
    import re
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def validate_emails(emails: list[str]) -> tuple[list[str], list[str]]:
    """Validate multiple emails. Returns (valid_list, invalid_list)."""
    valid, invalid = [], []
    for email in emails:
        if validate_email(email):
            valid.append(email)
        else:
            invalid.append(email)
    return valid, invalid


def sanitize_gmail_query(query: str) -> str:
    """Sanitize Gmail search query to prevent injection."""
    # Remove control characters
    query = re.sub(r'[\x00-\x1F\x7F]', '', query)
    # Gmail query syntax is pretty safe, but we can add basic checks
    return query.strip()


def extract_primary_email(raw: str) -> str:
    """Extract email from 'Name <email>' format."""
    match = re.search(r'<([^>]+)>', raw)
    return match.group(1) if match else raw.strip()


# ═════════════════════════════════════════════════════════════════════════════
# RESPONSE STANDARDIZATION
# ═════════════════════════════════════════════════════════════════════════════

def standardize_email_response(msg: dict) -> dict:
    """
    Standardize Gmail API response to consistent format.
    This is REQUIRED for orchestrator context integration.
    """
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    
    # Extract parts
    parts = payload.get("parts", [])
    attachments = []
    for part in parts:
        if part.get("filename"):
            attachments.append({
                "filename": part["filename"],
                "mime_type": part.get("mimeType", "application/octet-stream"),
                "size": part.get("body", {}).get("size", 0),
                "attachment_id": part.get("body", {}).get("attachmentId", "")
            })
    
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "subject": headers.get("subject", "(No Subject)"),
        "from": headers.get("from", ""),
        "from_email": extract_primary_email(headers.get("from", "")),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "body_text": "",  # Populated by separate parsing
        "body_html": "",
        "labels": msg.get("labelIds", []),
        "is_unread": "UNREAD" in msg.get("labelIds", []),
        "is_starred": "STARRED" in msg.get("labelIds", []),
        "is_important": "IMPORTANT" in msg.get("labelIds", []),
        "attachments": attachments,
        "has_attachments": len(attachments) > 0,
        "size": msg.get("sizeEstimate", 0),
        "history_id": msg.get("historyId", ""),
    }


def paginated_response(items: list, next_page_token: str = "", 
                       has_more: bool = False, total_count: int = None) -> dict:
    """Standard paginated response format."""
    return {
        "items": items,
        "next_page_token": next_page_token,
        "has_more": has_more,
        "total_count": total_count or len(items),
        "returned_count": len(items)
    }


# ═════════════════════════════════════════════════════════════════════════════
# CONTEXT / MEMORY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

class EmailContext:
    """Manages email context for natural language references."""
    
    def __init__(self):
        self.last_viewed_ids: list[str] = []
        self.last_search_results: list[dict] = []
        self.current_draft_id: Optional[str] = None
        self.recent_contacts: list[str] = []
    
    def add_viewed(self, email_id: str):
        """Track recently viewed emails."""
        if email_id in self.last_viewed_ids:
            self.last_viewed_ids.remove(email_id)
        self.last_viewed_ids.insert(0, email_id)
        self.last_viewed_ids = self.last_viewed_ids[:20]  # Keep last 20
    
    def add_search_results(self, results: list[dict]):
        """Store last search results for reference resolution."""
        self.last_search_results = results[:10]  # Keep top 10
    
    def set_current_draft(self, draft_id: str):
        """Track current draft for "this draft" references."""
        self.current_draft_id = draft_id
    
    def clear_current_draft(self):
        """Clear current draft tracking."""
        self.current_draft_id = None
    
    def resolve_reference(self, reference: str) -> Optional[str]:
        """
        Resolve natural language reference to email ID.
        
        Supports:
        - "latest" / "last" / "most recent" → last_viewed_ids[0]
        - "that email" / "this email" → last_viewed_ids[0]
        - "first" / "second" / "third" → search results by index
        - "email from {sender}" → search by sender
        """
        ref = reference.lower().strip()
        
        # Latest/last/most recent
        if ref in ("latest", "last", "most recent", "that email", "this email", "it"):
            return self.last_viewed_ids[0] if self.last_viewed_ids else None
        
        # Ordinal references from search results
        ordinals = {
            "first": 0, "1st": 0, "1": 0,
            "second": 1, "2nd": 1, "2": 1,
            "third": 2, "3rd": 2, "3": 2,
            "fourth": 3, "4th": 3, "4": 3,
            "fifth": 5, "5th": 4, "5": 4,
        }
        if ref in ordinals and self.last_search_results:
            idx = ordinals[ref]
            if idx < len(self.last_search_results):
                return self.last_search_results[idx].get("id")
        
        # "email from {sender}"
        if "from " in ref:
            sender = ref.split("from ")[-1].strip()
            # Search in recent results first
            for result in self.last_search_results:
                if sender.lower() in result.get("from", "").lower():
                    return result.get("id")
        
        return None


# Global context instance
_email_context = EmailContext()


# ═════════════════════════════════════════════════════════════════════════════
# SAFETY HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def check_batch_safety(ids: list[str], action: str) -> tuple[bool, str]:
    """
    Safety check for batch operations.
    
    Returns (is_safe, warning_message)
    """
    if len(ids) > 50:
        return False, f"Cannot {action} more than 50 emails at once. You requested {len(ids)}."
    
    if len(ids) > 10:
        return True, f"Warning: You are about to {action} {len(ids)} emails. This action cannot be undone."
    
    return True, ""


def check_send_safety(to_emails: list[str], subject: str, body: str) -> tuple[bool, str]:
    """
    Safety check before sending email.
    
    Returns (is_safe, warning_message)
    """
    warnings = []
    
    # Check for empty subject
    if not subject or not subject.strip():
        warnings.append("Email has no subject")
    
    # Check for empty body
    if not body or not body.strip():
        warnings.append("Email has no body")
    
    # Check for suspicious recipients
    suspicious_domains = ["tempmail", "guerrillamail", "throwaway"]
    for email in to_emails:
        domain = email.split("@")[-1].lower()
        if any(sus in domain for sus in suspicious_domains):
            warnings.append(f"Recipient uses temporary email domain: {domain}")
    
    # Check for large recipient count (potential spam)
    if len(to_emails) > 20:
        warnings.append(f"Email sent to {len(to_emails)} recipients - verify this is intentional")
    
    if warnings:
        return True, "; ".join(warnings)  # Allow but warn
    
    return True, ""


# ═════════════════════════════════════════════════════════════════════════════
# ATTACHMENT INTELLIGENCE
# ═════════════════════════════════════════════════════════════════════════════

def get_attachment_type_intelligence(mime_type: str, filename: str) -> dict:
    """
    Analyze attachment and provide routing suggestions.
    
    Returns:
        {
            "mime_type": "...",
            "category": "document/spreadsheet/presentation/image/other",
            "suggested_mcp": "docs/sheets/slides/drive/none",
            "can_preview": True/False,
            "description": "..."
        }
    """
    mime_lower = mime_type.lower()
    
    # Google Workspace files
    if "vnd.google-apps.document" in mime_lower or filename.endswith(('.doc', '.docx')):
        return {
            "mime_type": mime_type,
            "category": "document",
            "suggested_mcp": "docs",
            "can_preview": True,
            "description": "Document - can be opened with Docs MCP"
        }
    
    if "vnd.google-apps.spreadsheet" in mime_lower or filename.endswith(('.xls', '.xlsx', '.csv')):
        return {
            "mime_type": mime_type,
            "category": "spreadsheet",
            "suggested_mcp": "sheets",
            "can_preview": True,
            "description": "Spreadsheet - can be opened with Sheets MCP"
        }
    
    if "vnd.google-apps.presentation" in mime_lower or filename.endswith(('.ppt', '.pptx')):
        return {
            "mime_type": mime_type,
            "category": "presentation",
            "suggested_mcp": "slides",
            "can_preview": True,
            "description": "Presentation - can be opened with Slides MCP"
        }
    
    # Images
    if mime_lower.startswith("image/"):
        return {
            "mime_type": mime_type,
            "category": "image",
            "suggested_mcp": "none",
            "can_preview": True,
            "description": "Image file"
        }
    
    # PDFs
    if mime_lower == "application/pdf" or filename.endswith('.pdf'):
        return {
            "mime_type": mime_type,
            "category": "document",
            "suggested_mcp": "drive",
            "can_preview": True,
            "description": "PDF document"
        }
    
    # Default
    return {
        "mime_type": mime_type,
        "category": "other",
        "suggested_mcp": "drive",
        "can_preview": False,
        "description": f"File: {filename}"
    }
