"""
Gmail MCP v2 — Agent-Native Email Integration

Unified, production-grade Gmail tools with:
- ID resolution from natural language
- Unified action tools (reduce fragmentation)
- Pagination support
- Batch operations
- Safety layer
- Context-friendly responses
"""

from __future__ import annotations

import threading
import time
import base64
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from app.core.config import TOKEN_FILE, GMAIL_SCOPES as SCOPES

# Import utilities
from app.integrations.gmail.utils import (
    EmailError, EmailNotFoundError, EmailAmbiguityError, EmailPermissionError,
    EmailRateLimitError, EmailValidationError, EmailSafetyError,
    _gmail_cache, invalidate_cache, cached,
    _gmail_logger, log_tool_call,
    validate_email, validate_emails, sanitize_gmail_query, extract_primary_email,
    standardize_email_response, paginated_response,
    _email_context,
    check_batch_safety, check_send_safety,
    get_attachment_type_intelligence
)

_thread_local = threading.local()

# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═════════════════════════════════════════════════════════════════════════════

def authenticate_gmail():
    """Return thread-local Gmail API service."""
    if hasattr(_thread_local, "gmail_service") and _thread_local.gmail_service is not None:
        return _thread_local.gmail_service
    
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            TOKEN_FILE.unlink(missing_ok=True)
            creds = None
    if not creds or not creds.valid:
        raise PermissionError("Gmail not authenticated. Run `python auth.py` then restart.")
    
    service = build("gmail", "v1", credentials=creds)
    _thread_local.gmail_service = service
    return service


def _reset_service():
    if hasattr(_thread_local, "gmail_service"):
        _thread_local.gmail_service = None


def _gmail_api_call(api_callable, retries=3, backoff=1.5):
    """Execute Gmail API call with retry logic and error translation."""
    last_error = None
    for attempt in range(retries):
        try:
            return api_callable()
        except HttpError as exc:
            status = exc.resp.status if hasattr(exc, "resp") else 0
            if status == 401:
                _reset_service()
                last_error = exc
                continue
            if status == 404:
                raise EmailNotFoundError(f"Email or resource not found: {exc}")
            if status == 403:
                raise EmailPermissionError(f"Permission denied: {exc}")
            if status == 429:
                retry_after = int(exc.resp.headers.get("Retry-After", 60))
                raise EmailRateLimitError("Rate limit exceeded", retry_after=retry_after)
            if status in (500, 502, 503, 504):
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            raise EmailError(f"Gmail API error ({status}): {exc}")
        except (OSError, ConnectionError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise EmailError(f"Gmail API call failed after {retries} attempts: {last_error}")


# ═════════════════════════════════════════════════════════════════════════════
# ID RESOLUTION (Natural Language → ID)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def resolve_email_id(reference: str = "", use_context: bool = True) -> str:
    """
    Resolve natural language reference to email ID.
    
    Supports:
    - "latest", "last", "most recent" → most recently viewed
    - "that email", "this email" → last viewed
    - "first", "second", "third" → from last search results
    - "email from {sender}" → search by sender
    - Raw ID → returned as-is
    
    Args:
        reference: Natural language reference or email ID
        use_context: Whether to use conversation context
    
    Returns:
        Resolved email ID
    
    Raises:
        EmailAmbiguityError: If multiple emails match
        EmailNotFoundError: If no email found
    """
    # If looks like an ID (no spaces, alphanumeric), return as-is
    if reference and not any(c.isspace() for c in reference) and len(reference) > 10:
        return reference
    
    if use_context:
        # Try context resolution first
        resolved = _email_context.resolve_reference(reference or "latest")
        if resolved:
            return resolved
    
    # Fallback: search for it
    service = authenticate_gmail()
    
    # Search by sender if "from" mentioned
    if "from " in reference.lower():
        sender = reference.lower().split("from ")[-1].strip()
        results = _search_emails_internal(from_sender=sender, limit=5)
        if results:
            if len(results) == 1:
                return results[0]["id"]
            else:
                raise EmailAmbiguityError(
                    f"Multiple emails from '{sender}' found",
                    matches=[{"id": r["id"], "subject": r["subject"]} for r in results]
                )
    
    # Try subject search
    results = _search_emails_internal(query=reference, limit=5)
    if results:
        if len(results) == 1:
            return results[0]["id"]
        else:
            raise EmailAmbiguityError(
                f"Multiple emails match '{reference}'",
                matches=[{"id": r["id"], "subject": r["subject"], "from": r["from"]} for r in results]
            )
    
    raise EmailNotFoundError(f"Could not resolve email reference: '{reference}'")


@log_tool_call
def resolve_draft_id(reference: str = "", use_context: bool = True) -> str:
    """
    Resolve natural language reference to draft ID.
    
    Supports:
    - "this draft", "that draft", "it" → current draft from context
    - "latest", "last" → most recent draft
    - Raw draft ID → returned as-is
    
    Args:
        reference: Natural language reference or draft ID
        use_context: Whether to use conversation context
    
    Returns:
        Resolved draft ID
    """
    # If looks like an ID, return as-is
    if reference and not any(c.isspace() for c in reference) and len(reference) > 10:
        return reference
    
    if use_context:
        ref_lower = reference.lower().strip()
        if ref_lower in ("this draft", "that draft", "it", "current draft"):
            if _email_context.current_draft_id:
                return _email_context.current_draft_id
            raise EmailNotFoundError("No current draft in context")
        
        if ref_lower in ("latest", "last", "most recent"):
            # Get most recent draft
            drafts = list_drafts(limit=1)
            if drafts["items"]:
                return drafts["items"][0]["id"]
            raise EmailNotFoundError("No drafts found")
    
    raise EmailNotFoundError(f"Could not resolve draft reference: '{reference}'")


# ═════════════════════════════════════════════════════════════════════════════
# UNIFIED TOOLS (Reduce Fragmentation)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def email_action(
    action: str = "get",
    email_id: str = "",
    email_reference: str = "",
    query: str = "",
    thread_id: str = "",
    limit: int = 20,
    page_token: str = ""
) -> dict:
    """
    UNIFIED: Read, search, or get thread - replaces multiple tools.
    
    Args:
        action: "get", "search", "get_thread"
        email_id: Direct email ID (optional)
        email_reference: Natural language reference like "latest", "from John" (optional)
        query: Search query for search action
        thread_id: Thread ID for get_thread action
        limit: Max results for search
        page_token: Pagination token
    
    Examples:
        email_action(action="get", email_reference="latest")
        email_action(action="search", query="invoice from:amazon")
        email_action(action="get_thread", thread_id="...")
    """
    if action == "get":
        # Resolve reference if email_id not provided
        if not email_id and email_reference:
            email_id = resolve_email_id(email_reference)
        elif not email_id:
            raise EmailValidationError("email_id", "", "Either email_id or email_reference required")
        return get_email_by_id(email_id)
    
    elif action == "search":
        return search_emails(query=query, limit=limit, page_token=page_token)
    
    elif action == "get_thread":
        if not thread_id:
            # Try to get from email context
            if email_id:
                email = get_email_by_id(email_id)
                thread_id = email.get("thread_id")
            elif email_reference:
                email = email_action(action="get", email_reference=email_reference)
                thread_id = email.get("thread_id")
        if not thread_id:
            raise EmailValidationError("thread_id", "", "thread_id required for get_thread action")
        return get_email_thread(thread_id=thread_id)
    
    else:
        raise EmailValidationError("action", action, f"Unknown action: {action}. Use: get, search, get_thread")


@log_tool_call
def email_modify(
    action: str = "archive",
    email_id: str = "",
    email_reference: str = "",
    label: str = "",
    require_confirmation: bool = False
) -> dict:
    """
    UNIFIED: Archive, star, trash, mark read/unread, add/remove labels.
    
    Args:
        action: "archive", "star", "unstar", "trash", "restore", 
                "read", "unread", "add_label", "remove_label"
        email_id: Direct email ID
        email_reference: Natural language reference
        label: Label name (for add_label/remove_label)
        require_confirmation: If True, raises EmailSafetyError for destructive actions
    
    Examples:
        email_modify(action="archive", email_reference="latest")
        email_modify(action="star", email_id="...")
        email_modify(action="add_label", email_reference="from Boss", label="Important")
    """
    # Resolve email ID
    if not email_id and email_reference:
        email_id = resolve_email_id(email_reference)
    elif not email_id:
        raise EmailValidationError("email_id", "", "Either email_id or email_reference required")
    
    # Safety check for destructive actions
    destructive_actions = ["trash", "delete"]
    if require_confirmation and action in destructive_actions:
        raise EmailSafetyError(
            f"Destructive action '{action}' requires explicit confirmation. "
            "Set require_confirmation=False to proceed."
        )
    
    # Route to specific action
    action_map = {
        "archive": lambda: archive_email(email_id=email_id),
        "unarchive": lambda: unarchive_email(email_id=email_id),
        "star": lambda: star_email(email_id=email_id),
        "unstar": lambda: unstar_email(email_id=email_id),
        "trash": lambda: trash_email(email_id=email_id),
        "restore": lambda: restore_email(email_id=email_id),
        "read": lambda: mark_as_read(email_id=email_id),
        "unread": lambda: mark_as_unread(email_id=email_id),
        "add_label": lambda: add_label(email_id=email_id, label=label),
        "remove_label": lambda: remove_label(email_id=email_id, label=label),
    }
    
    if action not in action_map:
        raise EmailValidationError("action", action, f"Unknown action: {action}")
    
    result = action_map[action]()
    result["action_taken"] = action
    result["email_id"] = email_id
    return result


@log_tool_call
def email_generate(
    type: str = "reply",
    to: str = "",
    subject: str = "",
    body: str = "",
    email_id: str = "",
    email_reference: str = "",
    draft_id: str = "",
    draft_reference: str = "",
    send_now: bool = False,
    attachments: list = None
) -> dict:
    """
    UNIFIED: Reply, rewrite, forward, create draft, send draft.
    
    Args:
        type: "reply", "reply_all", "forward", "new", "send_draft", "update_draft"
        to: Recipient(s) for new emails
        subject: Subject line
        body: Email body
        email_id: Reference email for reply/forward
        email_reference: Natural language reference for reply/forward
        draft_id: Draft to send/update
        draft_reference: Natural language reference to draft
        send_now: If True, send immediately (for reply/new)
        attachments: List of attachment file paths
    
    Examples:
        email_generate(type="reply", email_reference="from John", body="Thanks!")
        email_generate(type="new", to="user@test.com", subject="Hello", body="...")
        email_generate(type="send_draft", draft_reference="this draft")
    """
    attachments = attachments or []
    
    if type == "reply":
        if not email_id and email_reference:
            email_id = resolve_email_id(email_reference)
        if not email_id:
            raise EmailValidationError("email_id", "", "email_id or email_reference required for reply")
        
        if send_now:
            return reply_email(email_id=email_id, body=body, attachments=attachments)
        else:
            return draft_reply(email_id=email_id, body=body)
    
    elif type == "reply_all":
        if not email_id and email_reference:
            email_id = resolve_email_id(email_reference)
        if not email_id:
            raise EmailValidationError("email_id", "", "email_id or email_reference required for reply_all")
        return reply_all(email_id=email_id, body=body)
    
    elif type == "forward":
        if not email_id and email_reference:
            email_id = resolve_email_id(email_reference)
        if not email_id:
            raise EmailValidationError("email_id", "", "email_id or email_reference required for forward")
        return forward_email(email_id=email_id, to=to, body=body)
    
    elif type == "new":
        # Safety check
        to_list = [e.strip() for e in to.split(",") if e.strip()]
        is_safe, warning = check_send_safety(to_list, subject, body)
        if not is_safe:
            raise EmailSafetyError(warning)
        
        if send_now:
            return send_email(to=to, subject=subject, body=body, attachments=attachments)
        else:
            return draft_email(to=to, subject=subject, body=body)
    
    elif type == "send_draft":
        if not draft_id and draft_reference:
            draft_id = resolve_draft_id(draft_reference)
        if not draft_id:
            raise EmailValidationError("draft_id", "", "draft_id or draft_reference required")
        return send_draft(draft_id=draft_id)
    
    elif type == "update_draft":
        if not draft_id and draft_reference:
            draft_id = resolve_draft_id(draft_reference)
        if not draft_id:
            raise EmailValidationError("draft_id", "", "draft_id or draft_reference required")
        return update_draft(draft_id=draft_id, subject=subject, body=body)
    
    else:
        raise EmailValidationError("type", type, f"Unknown type: {type}")


# ═════════════════════════════════════════════════════════════════════════════
# PAGINATED GETTERS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_emails(
    limit: int = 20,
    page_token: str = "",
    label: str = "",
    unread_only: bool = False,
    starred_only: bool = False,
    use_cache: bool = False
) -> dict:
    """
    Get emails with pagination support.
    
    Args:
        limit: Max emails to return (max 50)
        page_token: Next page token from previous call
        label: Filter by label (e.g., "INBOX", "SENT")
        unread_only: Only unread emails
        starred_only: Only starred emails
        use_cache: Whether to use cached results
    
    Returns:
        Paginated response with emails, next_page_token, has_more
    """
    if use_cache:
        cache_key = f"emails:{limit}:{label}:{unread_only}:{starred_only}:{page_token}"
        cached = _gmail_cache.get(cache_key)
        if cached:
            return cached
    
    service = authenticate_gmail()
    
    # Build query
    query_parts = []
    if unread_only:
        query_parts.append("is:unread")
    if starred_only:
        query_parts.append("is:starred")
    
    query = " ".join(query_parts) if query_parts else ""
    
    # Get label ID if specified
    label_ids = []
    if label:
        if label.upper() in ["INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "IMPORTANT", "STARRED", "UNREAD"]:
            label_ids = [label.upper()]
        else:
            # Need to look up custom label
            labels = list_labels()
            for lbl in labels.get("labels", []):
                if lbl.get("name", "").lower() == label.lower():
                    label_ids = [lbl.get("id")]
                    break
    
    params = {
        "userId": "me",
        "maxResults": min(limit, 50),
        "q": query,
    }
    if page_token:
        params["pageToken"] = page_token
    if label_ids:
        params["labelIds"] = label_ids
    
    result = _gmail_api_call(lambda: service.users().messages().list(**params).execute())
    
    messages = result.get("messages", [])
    next_token = result.get("nextPageToken", "")
    
    # Fetch full details for each message
    emails = []
    for msg in messages:
        try:
            full_msg = _gmail_api_call(
                lambda: service.users().messages().get(userId="me", id=msg["id"]).execute()
            )
            email_data = standardize_email_response(full_msg)
            emails.append(email_data)
            _email_context.add_viewed(msg["id"])
        except Exception:
            continue  # Skip problematic messages
    
    response = paginated_response(
        items=emails,
        next_page_token=next_token,
        has_more=bool(next_token),
        total_count=result.get("resultSizeEstimate", len(emails))
    )
    
    # Store in context for reference resolution
    _email_context.add_search_results(emails)
    
    if use_cache:
        _gmail_cache.set(cache_key, response)
    
    return response


@log_tool_call
def search_emails(
    query: str = "",
    from_sender: str = "",
    to_recipient: str = "",
    subject: str = "",
    date_from: str = "",
    date_to: str = "",
    has_attachment: bool = False,
    unread_only: bool = False,
    starred_only: bool = False,
    limit: int = 20,
    page_token: str = "",
    use_cache: bool = False
) -> dict:
    """
    Smart search with advanced filters and pagination.
    
    Args:
        query: Free text search
        from_sender: Filter by sender email/name
        to_recipient: Filter by recipient
        subject: Filter by subject (substring match)
        date_from: Start date (YYYY-MM-DD)
        date_to: End date (YYYY-MM-DD)
        has_attachment: Only emails with attachments
        unread_only: Only unread emails
        starred_only: Only starred emails
        limit: Max results (max 50)
        page_token: Pagination token
        use_cache: Whether to cache results
    
    Returns:
        Paginated response with matching emails
    """
    # Build Gmail search query
    query_parts = [query] if query else []
    
    if from_sender:
        query_parts.append(f"from:{from_sender}")
    if to_recipient:
        query_parts.append(f"to:{to_recipient}")
    if subject:
        query_parts.append(f"subject:{subject}")
    if date_from:
        query_parts.append(f"after:{date_from}")
    if date_to:
        query_parts.append(f"before:{date_to}")
    if has_attachment:
        query_parts.append("has:attachment")
    if unread_only:
        query_parts.append("is:unread")
    if starred_only:
        query_parts.append("is:starred")
    
    full_query = " ".join(query_parts)
    full_query = sanitize_gmail_query(full_query)
    
    if use_cache:
        cache_key = f"search:{full_query}:{limit}:{page_token}"
        cached = _gmail_cache.get(cache_key)
        if cached:
            return cached
    
    service = authenticate_gmail()
    
    params = {
        "userId": "me",
        "maxResults": min(limit, 50),
        "q": full_query,
    }
    if page_token:
        params["pageToken"] = page_token
    
    result = _gmail_api_call(lambda: service.users().messages().list(**params).execute())
    
    messages = result.get("messages", [])
    next_token = result.get("nextPageToken", "")
    
    # Fetch full details
    emails = []
    for msg in messages:
        try:
            full_msg = _gmail_api_call(
                lambda: service.users().messages().get(userId="me", id=msg["id"]).execute()
            )
            email_data = standardize_email_response(full_msg)
            emails.append(email_data)
            _email_context.add_viewed(msg["id"])
        except Exception:
            continue
    
    response = paginated_response(
        items=emails,
        next_page_token=next_token,
        has_more=bool(next_token),
        total_count=result.get("resultSizeEstimate", len(emails))
    )
    
    # Store in context
    _email_context.add_search_results(emails)
    
    if use_cache:
        _gmail_cache.set(cache_key, response)
    
    return response


# Internal helper for resolution
@cached("search", ttl=60)
def _search_emails_internal(from_sender: str = "", query: str = "", limit: int = 5) -> list:
    """Internal search for ID resolution with caching."""
    result = search_emails(from_sender=from_sender, query=query, limit=limit, use_cache=True)
    return result.get("items", [])


# ═════════════════════════════════════════════════════════════════════════════
# BATCH OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def batch_email_action(
    action: str = "archive",
    email_ids: list = None,
    email_references: list = None,
    require_confirmation: bool = True
) -> dict:
    """
    Batch operations on multiple emails.
    
    Args:
        action: "archive", "star", "unstar", "trash", "restore", "read", "unread", "delete"
        email_ids: List of email IDs
        email_references: List of natural language references
        require_confirmation: If True, raises safety error for >10 items
    
    Returns:
        Summary of batch operation results
    """
    email_ids = email_ids or []
    email_references = email_references or []
    
    # Resolve all references to IDs
    all_ids = list(email_ids)
    for ref in email_references:
        try:
            resolved = resolve_email_id(ref)
            all_ids.append(resolved)
        except EmailNotFoundError:
            pass  # Skip unresolvable
    
    # Remove duplicates
    all_ids = list(set(all_ids))
    
    # Safety check
    is_safe, warning = check_batch_safety(all_ids, action)
    if not is_safe:
        raise EmailSafetyError(warning)
    if require_confirmation and len(all_ids) > 10:
        raise EmailSafetyError(
            f"Batch {action} of {len(all_ids)} emails requires confirmation. "
            f"Set require_confirmation=False to proceed. Warning: {warning}"
        )
    
    results = {"success": [], "failed": []}
    
    for email_id in all_ids:
        try:
            email_modify(action=action, email_id=email_id, require_confirmation=False)
            results["success"].append(email_id)
        except Exception as e:
            results["failed"].append({"id": email_id, "error": str(e)})
    
    invalidate_cache("search")
    invalidate_cache("emails")
    
    return {
        "action": action,
        "total": len(all_ids),
        "succeeded": len(results["success"]),
        "failed": len(results["failed"]),
        "results": results,
        "warning": warning if warning else None
    }


# Aliases for specific batch operations
@log_tool_call
def archive_emails(email_ids: list = None, email_references: list = None) -> dict:
    """Batch archive emails."""
    return batch_email_action("archive", email_ids, email_references)


@log_tool_call
def trash_emails(email_ids: list = None, email_references: list = None) -> dict:
    """Batch trash emails."""
    return batch_email_action("trash", email_ids, email_references)


@log_tool_call
def delete_emails(email_ids: list = None, email_references: list = None) -> dict:
    """Batch permanently delete emails."""
    return batch_email_action("delete", email_ids, email_references)


@log_tool_call
def mark_emails_read(email_ids: list = None, email_references: list = None) -> dict:
    """Batch mark emails as read."""
    return batch_email_action("read", email_ids, email_references)


@log_tool_call
def star_emails(email_ids: list = None, email_references: list = None) -> dict:
    """Batch star emails."""
    return batch_email_action("star", email_ids, email_references)


# ═════════════════════════════════════════════════════════════════════════════
# AI / ANALYSIS UNIFIED
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def email_analyze(
    type: str = "summary",
    email_id: str = "",
    email_reference: str = "",
    query: str = "",
    ai_model: str = "default"
) -> dict:
    """
    UNIFIED: Summarize, detect urgency, extract tasks, sentiment analysis.
    
    Args:
        type: "summary", "urgency", "tasks", "sentiment", "classify"
        email_id: Direct email ID
        email_reference: Natural language reference
        query: For context when analyzing
        ai_model: AI model to use for analysis
    
    Examples:
        email_analyze(type="summary", email_reference="latest")
        email_analyze(type="urgency", email_id="...")
        email_analyze(type="tasks", email_reference="from Boss")
    """
    # Import AI tools dynamically
    from app.integrations.gmail import ai
    
    # Resolve email
    if not email_id and email_reference:
        email_id = resolve_email_id(email_reference)
    elif not email_id and query:
        # Search for email
        results = search_emails(query=query, limit=1)
        if results["items"]:
            email_id = results["items"][0]["id"]
    
    if not email_id:
        raise EmailValidationError("email_id", "", "email_id, email_reference, or query required")
    
    # Get email content
    email_data = get_email_by_id(email_id)
    
    # Route to AI function
    if type == "summary":
        summary = ai.summarize_email(email_data)
        return {"email_id": email_id, "analysis_type": "summary", "result": summary}
    
    elif type == "urgency":
        urgency = ai.detect_urgency(email_data)
        return {"email_id": email_id, "analysis_type": "urgency", "result": urgency}
    
    elif type == "tasks":
        tasks = ai.extract_tasks(email_data)
        return {"email_id": email_id, "analysis_type": "tasks", "result": tasks}
    
    elif type == "sentiment":
        sentiment = ai.sentiment_analysis(email_data)
        return {"email_id": email_id, "analysis_type": "sentiment", "result": sentiment}
    
    elif type == "classify":
        classification = ai.classify_email(email_data)
        return {"email_id": email_id, "analysis_type": "classify", "result": classification}
    
    else:
        raise EmailValidationError("type", type, f"Unknown analysis type: {type}")


# ═════════════════════════════════════════════════════════════════════════════
# ORIGINAL TOOL IMPLEMENTATIONS (for backward compatibility)
# ═════════════════════════════════════════════════════════════════════════════

# These are wrappers that call the original core.py functions
# to maintain backward compatibility during migration

from app.integrations.gmail import core as _orig_core


def get_email_by_id(email_id: str) -> dict:
    """Get single email by ID with standardized response."""
    result = _orig_core.get_email_by_id(email_id)
    if result:
        _email_context.add_viewed(email_id)
    return result


def get_email_thread(thread_id: str) -> dict:
    """Get full email thread."""
    return _orig_core.get_email_thread(thread_id)


def send_email(to: str, subject: str, body: str, attachments: list = None) -> dict:
    """Send email with safety validation."""
    attachments = attachments or []
    
    # Validate recipients
    to_list = [e.strip() for e in to.split(",") if e.strip()]
    valid, invalid = validate_emails(to_list)
    if invalid:
        raise EmailValidationError("to", to, f"Invalid emails: {invalid}")
    
    is_safe, warning = check_send_safety(valid, subject, body)
    if not is_safe:
        raise EmailSafetyError(warning)
    
    return _orig_core.send_email(to=to, subject=subject, body=body, attachments=attachments)


def draft_email(to: str, subject: str, body: str) -> dict:
    """Create email draft."""
    result = _orig_core.draft_email(to=to, subject=subject, body=body)
    if result and result.get("id"):
        _email_context.set_current_draft(result["id"])
    return result


def send_draft(draft_id: str) -> dict:
    """Send existing draft."""
    result = _orig_core.send_draft(draft_id=draft_id)
    if result.get("success"):
        _email_context.clear_current_draft()
    return result


def update_draft(draft_id: str, subject: str = "", body: str = "") -> dict:
    """Update existing draft."""
    return _orig_core.update_draft(draft_id=draft_id, subject=subject, body=body)


def reply_email(email_id: str, body: str, attachments: list = None) -> dict:
    """Reply to email."""
    attachments = attachments or []
    return _orig_core.reply_email(email_id=email_id, body=body, attachments=attachments)


def reply_all(email_id: str, body: str) -> dict:
    """Reply all to email."""
    return _orig_core.reply_all(email_id=email_id, body=body)


def forward_email(email_id: str, to: str, body: str = "") -> dict:
    """Forward email."""
    return _orig_core.forward_email(email_id=email_id, to=to, body=body)


def draft_reply(email_id: str, body: str) -> dict:
    """Create draft reply."""
    result = _orig_core.draft_reply(email_id=email_id, body=body)
    if result and result.get("id"):
        _email_context.set_current_draft(result["id"])
    return result


def archive_email(email_id: str) -> dict:
    """Archive email."""
    return _orig_core.archive_email(email_id=email_id)


def unarchive_email(email_id: str) -> dict:
    """Unarchive email (move to inbox)."""
    return _orig_core.unarchive_email(email_id=email_id)


def star_email(email_id: str) -> dict:
    """Star email."""
    return _orig_core.star_email(email_id=email_id)


def unstar_email(email_id: str) -> dict:
    """Unstar email."""
    return _orig_core.unstar_email(email_id=email_id)


def trash_email(email_id: str) -> dict:
    """Move email to trash."""
    return _orig_core.trash_email(email_id=email_id)


def restore_email(email_id: str) -> dict:
    """Restore email from trash."""
    return _orig_core.restore_email(email_id=email_id)


def mark_as_read(email_id: str) -> dict:
    """Mark email as read."""
    return _orig_core.mark_as_read(email_id=email_id)


def mark_as_unread(email_id: str) -> dict:
    """Mark email as unread."""
    return _orig_core.mark_as_unread(email_id=email_id)


def add_label(email_id: str, label: str) -> dict:
    """Add label to email."""
    return _orig_core.add_label(email_id=email_id, label=label)


def remove_label(email_id: str, label: str) -> dict:
    """Remove label from email."""
    return _orig_core.remove_label(email_id=email_id, label=label)


def list_labels() -> dict:
    """List all Gmail labels."""
    return _orig_core.list_labels()


def list_drafts(limit: int = 10) -> dict:
    """List email drafts."""
    return _orig_core.list_drafts(limit=limit)


def get_attachments(email_id: str) -> dict:
    """Get attachment metadata with intelligence."""
    result = _orig_core.get_attachments(email_id=email_id)
    
    # Add intelligence to attachments
    if result and "attachments" in result:
        for att in result["attachments"]:
            intelligence = get_attachment_type_intelligence(
                att.get("mime_type", ""),
                att.get("filename", "")
            )
            att["intelligence"] = intelligence
    
    return result


def download_attachment(message_id: str, attachment_id: str, filename: str) -> dict:
    """Download attachment."""
    return _orig_core.download_attachment(
        message_id=message_id,
        attachment_id=attachment_id,
        filename=filename
    )


# ═════════════════════════════════════════════════════════════════════════════
# STATISTICS & UTILITY
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_gmail_stats() -> dict:
    """Get Gmail MCP usage statistics."""
    return {
        "logger_stats": _gmail_logger.get_stats(),
        "cache_stats": _gmail_cache.stats(),
        "context": {
            "last_viewed_count": len(_email_context.last_viewed_ids),
            "last_search_count": len(_email_context.last_search_results),
            "current_draft": _email_context.current_draft_id
        }
    }


@log_tool_call
def clear_gmail_cache(prefix: str = "") -> dict:
    """Clear Gmail cache."""
    return invalidate_cache(prefix)
