"""
Drive MCP Utilities: Error classes, caching, logging, and helpers.
"""

from __future__ import annotations

import time
import logging
import functools
from typing import Any, Optional, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

# Configure logging
logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# ERROR CLASSES (Structured Error Handling)
# ═════════════════════════════════════════════════════════════════════════════

class DriveError(Exception):
    """Base exception for Drive MCP."""
    def __init__(self, message: str, code: str = "unknown", details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.timestamp = datetime.utcnow().isoformat()


class DriveNotFoundError(DriveError):
    """File or folder not found."""
    def __init__(self, resource_id: str = "", resource_type: str = "file"):
        super().__init__(
            message=f"{resource_type} not found: {resource_id}",
            code="not_found",
            details={"resource_id": resource_id, "resource_type": resource_type}
        )


class DrivePermissionError(DriveError):
    """Permission denied or invalid."""
    def __init__(self, action: str = "", reason: str = ""):
        super().__init__(
            message=f"Permission denied for action '{action}': {reason}",
            code="permission_denied",
            details={"action": action, "reason": reason}
        )


class DriveRateLimitError(DriveError):
    """API rate limit exceeded."""
    def __init__(self, retry_after: int = 60):
        super().__init__(
            message=f"Rate limit exceeded. Retry after {retry_after}s",
            code="rate_limit",
            details={"retry_after": retry_after}
        )


class DriveValidationError(DriveError):
    """Invalid input validation."""
    def __init__(self, field: str = "", value: Any = None, constraint: str = ""):
        super().__init__(
            message=f"Validation failed for '{field}': {constraint}",
            code="validation_error",
            details={"field": field, "value": str(value), "constraint": constraint}
        )


class DriveAmbiguityError(DriveError):
    """Multiple matches found when expecting one."""
    def __init__(self, name: str = "", matches: list = None):
        super().__init__(
            message=f"Multiple matches found for '{name}'",
            code="ambiguous_match",
            details={"name": name, "matches": matches or []}
        )


# ═════════════════════════════════════════════════════════════════════════════
# LIGHTWEIGHT CACHE WITH TTL
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class CacheEntry:
    """Cache entry with value and expiration time."""
    value: Any
    expires_at: float


class SimpleCache:
    """Lightweight in-memory cache with TTL support."""
    
    def __init__(self, default_ttl: int = 300):  # 5 minutes default
        self._cache: dict[str, CacheEntry] = {}
        self._default_ttl = default_ttl
        self._hits = 0
        self._misses = 0
    
    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        
        if time.time() > entry.expires_at:
            del self._cache[key]
            self._misses += 1
            return None
        
        self._hits += 1
        return entry.value
    
    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store value in cache with TTL."""
        ttl = ttl or self._default_ttl
        self._cache[key] = CacheEntry(
            value=value,
            expires_at=time.time() + ttl
        )
    
    def delete(self, key: str) -> None:
        """Remove key from cache."""
        self._cache.pop(key, None)
    
    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()
        self._hits = 0
        self._misses = 0
    
    def stats(self) -> dict:
        """Return cache statistics."""
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0
        return {
            "size": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1%}"
        }
    
    def cleanup_expired(self) -> int:
        """Remove expired entries and return count removed."""
        now = time.time()
        expired = [k for k, v in self._cache.items() if now > v.expires_at]
        for k in expired:
            del self._cache[k]
        return len(expired)


# Global cache instance
_drive_cache = SimpleCache(default_ttl=300)


def cached(ttl: int = 300, key_prefix: str = ""):
    """Decorator to cache function results."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Build cache key
            cache_key = f"{key_prefix}:{func.__name__}:{str(args)}:{str(kwargs)}"
            
            # Try cache first
            cached_value = _drive_cache.get(cache_key)
            if cached_value is not None:
                return cached_value
            
            # Execute and cache result
            result = func(*args, **kwargs)
            _drive_cache.set(cache_key, result, ttl)
            return result
        
        return wrapper
    return decorator


def invalidate_cache(pattern: str = "") -> None:
    """Invalidate cache entries matching pattern."""
    if pattern:
        keys_to_delete = [k for k in _drive_cache._cache.keys() if pattern in k]
        for k in keys_to_delete:
            _drive_cache.delete(k)
    else:
        _drive_cache.clear()


# ═════════════════════════════════════════════════════════════════════════════
# STRUCTURED LOGGING / OBSERVABILITY
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class ToolCallLog:
    """Structured log entry for tool calls."""
    tool_name: str
    arguments: dict
    result: Any
    latency_ms: float
    success: bool
    error: Optional[str] = None
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.utcnow().isoformat()
    
    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "result": self.result if self.success else None,
            "latency_ms": round(self.latency_ms, 2),
            "success": self.success,
            "error": self.error,
            "timestamp": self.timestamp,
        }


class DriveLogger:
    """Structured logging for Drive MCP operations."""
    
    def __init__(self):
        self._recent_calls: list[ToolCallLog] = []
        self._max_history = 100
    
    def log_call(
        self,
        tool_name: str,
        arguments: dict,
        result: Any,
        latency_ms: float,
        success: bool,
        error: Optional[str] = None
    ) -> None:
        """Log a tool call with structured data."""
        log_entry = ToolCallLog(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            latency_ms=latency_ms,
            success=success,
            error=error
        )
        
        self._recent_calls.append(log_entry)
        if len(self._recent_calls) > self._max_history:
            self._recent_calls.pop(0)
        
        # Also log to standard logger
        status = "SUCCESS" if success else "FAILED"
        logger.info(
            f"[Drive MCP] {status}: {tool_name} ({latency_ms:.2f}ms) "
            f"args={arguments} error={error or 'None'}"
        )
    
    def get_recent_calls(self, limit: int = 10) -> list[dict]:
        """Get recent tool call logs."""
        return [c.to_dict() for c in self._recent_calls[-limit:]]
    
    def get_stats(self) -> dict:
        """Get aggregated statistics."""
        if not self._recent_calls:
            return {"total_calls": 0, "success_rate": "0%", "avg_latency_ms": 0}
        
        total = len(self._recent_calls)
        successful = sum(1 for c in self._recent_calls if c.success)
        avg_latency = sum(c.latency_ms for c in self._recent_calls) / total
        
        return {
            "total_calls": total,
            "success_rate": f"{successful / total:.1%}",
            "avg_latency_ms": round(avg_latency, 2),
        }


# Global logger instance
_drive_logger = DriveLogger()


def log_tool_call(func: Callable) -> Callable:
    """Decorator to automatically log tool calls."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        tool_name = func.__name__
        
        # Build arguments dict (excluding sensitive data)
        arguments = {}
        if args:
            arguments["_args"] = str(args)
        for k, v in kwargs.items():
            if k not in ("password", "token", "credentials"):
                arguments[k] = str(v)[:100]  # Truncate long values
        
        try:
            result = func(*args, **kwargs)
            latency_ms = (time.time() - start_time) * 1000
            
            _drive_logger.log_call(
                tool_name=tool_name,
                arguments=arguments,
                result="success",
                latency_ms=latency_ms,
                success=True
            )
            return result
            
        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            
            _drive_logger.log_call(
                tool_name=tool_name,
                arguments=arguments,
                result=None,
                latency_ms=latency_ms,
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


def is_dangerous_role(role: str) -> tuple[bool, str]:
    """Check if sharing role is potentially dangerous."""
    dangerous_roles = {
        "owner": "Ownership transfer is not allowed via assistant",
    }
    
    role_lower = role.lower()
    if role_lower in dangerous_roles:
        return True, dangerous_roles[role_lower]
    return False, ""


def sanitize_drive_query(query: str) -> str:
    """Sanitize user input for Drive query strings."""
    # Escape single quotes by doubling them (Drive query syntax)
    return query.replace("'", "\\'")


# ═════════════════════════════════════════════════════════════════════════════
# RESPONSE STANDARDIZATION
# ═════════════════════════════════════════════════════════════════════════════

def standardize_file_response(file_data: dict) -> dict:
    """Ensure consistent response format for files."""
    mime = file_data.get("mimeType", "")
    
    return {
        "id": file_data.get("id", ""),
        "name": file_data.get("name", "(Untitled)"),
        "type": _get_type_label(mime),
        "mime": mime,
        "url": file_data.get("webViewLink") or file_data.get("webContentLink", ""),
        "parent": file_data.get("parents", [""])[0] if file_data.get("parents") else "",
        "modified_time": file_data.get("modifiedTime", ""),
        "size": file_data.get("size", ""),
        "starred": file_data.get("starred", False),
        "trashed": file_data.get("trashed", False),
    }


def _get_type_label(mime: str) -> str:
    """Get human-readable type label from MIME type."""
    type_map = {
        "application/vnd.google-apps.document": "Google Doc",
        "application/vnd.google-apps.spreadsheet": "Google Sheet",
        "application/vnd.google-apps.presentation": "Google Slides",
        "application/vnd.google-apps.form": "Google Form",
        "application/vnd.google-apps.folder": "Folder",
        "application/pdf": "PDF",
        "image/png": "PNG Image",
        "image/jpeg": "JPEG Image",
        "text/plain": "Text File",
        "text/csv": "CSV File",
        "application/zip": "ZIP Archive",
    }
    return type_map.get(mime, mime.split("/")[-1].upper() if mime else "Unknown")


def get_mcp_suggestion(mime_type: str) -> Optional[str]:
    """Suggest appropriate MCP based on file type."""
    suggestions = {
        "application/vnd.google-apps.document": "docs",
        "application/vnd.google-apps.spreadsheet": "sheets",
        "application/vnd.google-apps.presentation": "slides",
        "application/vnd.google-apps.form": "forms",
    }
    return suggestions.get(mime_type)


# ═════════════════════════════════════════════════════════════════════════════
# PAGINATION HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def paginated_response(
    items: list,
    next_page_token: Optional[str] = None,
    total_count: Optional[int] = None
) -> dict:
    """Build standardized paginated response."""
    return {
        "files": items,
        "next_page_token": next_page_token,
        "total_count": total_count or len(items),
        "has_more": bool(next_page_token),
    }


# ═════════════════════════════════════════════════════════════════════════════
# EXPORTS
# ═════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Errors
    "DriveError",
    "DriveNotFoundError",
    "DrivePermissionError",
    "DriveRateLimitError",
    "DriveValidationError",
    "DriveAmbiguityError",
    # Cache
    "SimpleCache",
    "cached",
    "invalidate_cache",
    "_drive_cache",
    # Logging
    "DriveLogger",
    "ToolCallLog",
    "log_tool_call",
    "_drive_logger",
    # Validation
    "validate_email",
    "is_dangerous_role",
    "sanitize_drive_query",
    # Response helpers
    "standardize_file_response",
    "get_mcp_suggestion",
    "paginated_response",
]
