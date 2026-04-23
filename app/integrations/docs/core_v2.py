"""
Google Docs MCP v2 — Agent-Native Document Integration

Unified, production-grade Docs tools with:
- ID resolution from natural language
- Unified action tools (reduce fragmentation)
- Section-level operations
- Content intelligence
- Pagination support
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from app.core.config import TOKEN_FILE, DOCS_SCOPES as SCOPES
from app.core.llm_client import call_model

# Import utilities
from app.integrations.docs.utils import (
    DocError, DocNotFoundError, DocAmbiguityError, DocPermissionError,
    DocRateLimitError, DocValidationError, DocSafetyError,
    _docs_cache, invalidate_cache, cached,
    _docs_logger, log_tool_call,
    validate_doc_title, sanitize_search_query, extract_doc_id_from_url,
    standardize_doc_response, paginated_response,
    _doc_context,
    check_content_safety, check_replace_safety, check_delete_batch_safety,
    LARGE_CONTENT_THRESHOLD, BATCH_SECTION_LIMIT, BATCH_REPLACE_LIMIT,
    extract_sections, get_content_type_intelligence
)

_thread_local = threading.local()

# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═════════════════════════════════════════════════════════════════════════════

def authenticate_docs():
    """Return thread-local Docs API service."""
    if hasattr(_thread_local, "docs_service") and _thread_local.docs_service is not None:
        return _thread_local.docs_service
    
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
        raise PermissionError("Google Docs not authenticated. Run `python auth.py` then restart.")
    
    service = build("docs", "v1", credentials=creds)
    _thread_local.docs_service = service
    return service


def authenticate_drive():
    """Return thread-local Drive API service (for metadata/search)."""
    if hasattr(_thread_local, "drive_service") and _thread_local.drive_service is not None:
        return _thread_local.drive_service
    
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
        raise PermissionError("Google Drive not authenticated. Run `python auth.py` then restart.")
    
    service = build("drive", "v3", credentials=creds)
    _thread_local.drive_service = service
    return service


def _reset_services():
    """Reset all cached services."""
    if hasattr(_thread_local, "docs_service"):
        _thread_local.docs_service = None
    if hasattr(_thread_local, "drive_service"):
        _thread_local.drive_service = None


def _docs_api_call(api_callable, retries=3, backoff=1.5):
    """Execute Docs API call with retry logic and error translation."""
    last_error = None
    for attempt in range(retries):
        try:
            return api_callable()
        except HttpError as exc:
            status = exc.resp.status if hasattr(exc, "resp") else 0
            if status == 401:
                _reset_services()
                last_error = exc
                continue
            if status == 404:
                raise DocNotFoundError(f"Document not found: {exc}")
            if status == 403:
                raise DocPermissionError(f"Permission denied: {exc}")
            if status == 429:
                retry_after = int(exc.resp.headers.get("Retry-After", 60))
                raise DocRateLimitError("Rate limit exceeded", retry_after=retry_after)
            if status in (500, 502, 503, 504):
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            raise DocError(f"Docs API error ({status}): {exc}")
        except (OSError, ConnectionError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise DocError(f"Docs API call failed after {retries} attempts: {last_error}")


# ═════════════════════════════════════════════════════════════════════════════
# ID RESOLUTION (Natural Language → Document ID)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def resolve_doc_id(reference: str = "", use_context: bool = True, 
                   use_drive_fallback: bool = True) -> str:
    """
    Resolve natural language reference to document ID.
    
    Supports:
    - "latest", "last", "most recent" → most recently viewed
    - "that document", "this doc", "it" → current document in context
    - "first", "second", "third" → from last search results
    - "document named {name}" → search by title
    - Raw ID or URL → extracted ID
    
    Args:
        reference: Natural language reference or document ID/URL
        use_context: Whether to use conversation context
        use_drive_fallback: Whether to search Drive if not in context
    
    Returns:
        Resolved document ID
    
    Raises:
        DocAmbiguityError: If multiple documents match
        DocNotFoundError: If no document found
    """
    # If looks like a Google Docs URL, extract ID
    if reference.startswith("https://"):
        doc_id = extract_doc_id_from_url(reference)
        if doc_id:
            return doc_id
    
    # If looks like an ID (alphanumeric, 25-50 chars), return as-is
    if reference and not any(c.isspace() for c in reference):
        if 25 <= len(reference) <= 50 and reference.replace('-', '').replace('_', '').isalnum():
            return reference
    
    if use_context:
        # Try context resolution first
        resolved = _doc_context.resolve_reference(reference or "latest")
        if resolved:
            return resolved
    
    # Fallback: Search Drive
    if use_drive_fallback:
        drive = authenticate_drive()
        
        # Try to extract document name from reference
        doc_name = reference
        if "named " in reference.lower():
            doc_name = reference.lower().split("named ")[-1].strip()
        
        safe_name = sanitize_search_query(doc_name)
        query = f"mimeType='application/vnd.google-apps.document' and name contains '{safe_name}' and trashed=false"
        
        results = _docs_api_call(lambda: drive.files().list(
            q=query,
            pageSize=5,
            fields="files(id, name, modifiedTime, owners)"
        ).execute())
        
        files = results.get("files", [])
        if files:
            if len(files) == 1:
                return files[0]["id"]
            else:
                # Multiple matches
                raise DocAmbiguityError(
                    f"Multiple documents match '{reference}'",
                    matches=[{"id": f["id"], "title": f["name"]} for f in files]
                )
    
    raise DocNotFoundError(f"Could not resolve document reference: '{reference}'")


# ═════════════════════════════════════════════════════════════════════════════
# UNIFIED TOOLS (Reduce Fragmentation)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def doc_action(
    action: str = "get",
    doc_id: str = "",
    doc_reference: str = "",
    query: str = "",
    title: str = "",
    date_from: str = "",
    date_to: str = "",
    owner: str = "",
    folder_id: str = "",
    folder_name: str = "",
    recently_modified: bool = False,
    shared_with_me: bool = False,
    limit: int = 20,
    page_token: str = ""
) -> dict:
    """
    UNIFIED: Read, search, or list documents.

    Args:
        action: "get", "read", "list", "search"
        doc_id: Direct document ID
        doc_reference: Natural language reference
        query: Search query (title contains)
        title: Alias for query
        date_from / date_to: ISO date range filter (search only)
        owner: Owner email filter (search only)
        folder_id / folder_name: Folder scope (search only)
        recently_modified: Last 7 days only (search only)
        shared_with_me: Shared-with-me filter (search only)
        limit: Max results for list/search
        page_token: Pagination token
    """
    if action == "get":
        if not doc_id and doc_reference:
            doc_id = resolve_doc_id(doc_reference)
        elif not doc_id:
            raise DocValidationError("doc_id", "", "Either doc_id or doc_reference required")
        return get_document_metadata(doc_id)

    elif action == "read":
        if not doc_id and doc_reference:
            doc_id = resolve_doc_id(doc_reference)
        elif not doc_id:
            raise DocValidationError("doc_id", "", "Either doc_id or doc_reference required")
        return read_document(doc_id, limit=limit, page_token=page_token)

    elif action == "list":
        return list_documents(limit=limit, page_token=page_token)

    elif action == "search":
        return search_documents(
            query=query,
            title=title,
            date_from=date_from,
            date_to=date_to,
            owner=owner,
            folder_id=folder_id,
            folder_name=folder_name,
            recently_modified=recently_modified,
            shared_with_me=shared_with_me,
            limit=limit,
            page_token=page_token
        )

    else:
        raise DocValidationError("action", action, f"Unknown action: {action}. Use: get, read, list, search")


@log_tool_call
def doc_modify(
    action: str = "append",
    doc_id: str = "",
    doc_reference: str = "",
    content: str = "",
    section: str = "",
    insert_index: int = None,
    require_confirmation: bool = False
) -> dict:
    """
    UNIFIED: Modify document content.
    
    Args:
        action: "append", "prepend", "replace", "insert", "delete_section", "clear"
        doc_id: Direct document ID
        doc_reference: Natural language reference
        content: Text content to insert
        section: Section/heading name (for replace/delete_section)
        insert_index: Character index for insert action
        require_confirmation: If True, raises DocSafetyError for destructive actions
    
    Examples:
        doc_modify(action="append", doc_reference="latest", content="New text")
        doc_modify(action="replace", doc_id="...", section="Introduction", content="New intro")
        doc_modify(action="delete_section", doc_reference="report", section="Old Section")
    """
    # Resolve document ID
    if not doc_id and doc_reference:
        doc_id = resolve_doc_id(doc_reference)
    elif not doc_id:
        raise DocValidationError("doc_id", "", "Either doc_id or doc_reference required")
    
    # Safety check for destructive actions
    destructive_actions = ["clear", "delete_section"]
    if action in destructive_actions:
        is_safe, warning = check_content_safety("", action)
        if not is_safe and require_confirmation:
            raise DocSafetyError(f"Destructive action '{action}' requires confirmation: {warning}")
    
    # Route to specific action
    if action == "append":
        return append_content(doc_id=doc_id, content=content)
    elif action == "prepend":
        return insert_content(doc_id=doc_id, content=content, index=1)  # After title
    elif action == "replace":
        if not section:
            raise DocValidationError("section", "", "section required for replace action")
        needs_confirm, warning = check_replace_safety(content, require_confirmation=require_confirmation)
        if needs_confirm:
            raise DocSafetyError(f"Replace safety check failed: {warning}")
        return replace_section(doc_id=doc_id, section_title=section, new_content=content)
    elif action == "insert":
        if insert_index is None:
            raise DocValidationError("insert_index", None, "insert_index required for insert action")
        return insert_content(doc_id=doc_id, content=content, index=insert_index)
    elif action == "delete_section":
        if not section:
            raise DocValidationError("section", "", "section required for delete_section action")
        return delete_section(doc_id=doc_id, section_title=section, require_confirmation=require_confirmation)
    elif action == "clear":
        return clear_document(doc_id=doc_id, require_confirmation=require_confirmation)
    else:
        raise DocValidationError("action", action, f"Unknown action: {action}")


@log_tool_call
def doc_create(
    title: str = "",
    content: str = "",
    from_template: str = "",
    template_id: str = ""
) -> dict:
    """
    UNIFIED: Create new document.
    
    Args:
        title: Document title
        content: Initial content
        from_template: Template name ("blank", "meeting_notes", "report")
        template_id: Google Docs template ID
    
    Returns:
        Created document metadata
    """
    # Validate title
    is_valid, error = validate_doc_title(title)
    if not is_valid:
        raise DocValidationError("title", title, error)
    
    if template_id:
        return create_from_template(title=title, template_id=template_id)
    elif from_template and from_template != "blank":
        return create_from_template(title=title, template_name=from_template)
    else:
        return create_blank_document(title=title, content=content)


@log_tool_call
def doc_analyze(
    type: str = "summary",
    doc_id: str = "",
    doc_reference: str = "",
    content: str = ""
) -> dict:
    """
    UNIFIED: Analyze document content.
    
    Args:
        type: "summary", "structure", "key_points", "action_items", "word_count"
        doc_id: Direct document ID
        doc_reference: Natural language reference
        content: Content to analyze (if doc_id not provided)
    
    Returns:
        Analysis results
    """
    # Get content to analyze
    if not content and (doc_id or doc_reference):
        if not doc_id and doc_reference:
            doc_id = resolve_doc_id(doc_reference)
        doc_data = read_document(doc_id, limit=10000)
        content = doc_data.get("content", "")
    
    if not content:
        raise DocValidationError("content", "", "Either content or doc_id/doc_reference required")
    
    # Route to analysis type
    if type == "summary":
        return analyze_summary(content)
    elif type == "structure":
        return analyze_structure(content)
    elif type == "key_points":
        return analyze_key_points(content)
    elif type == "action_items":
        return analyze_action_items(content)
    elif type == "word_count":
        return analyze_word_count(content)
    else:
        raise DocValidationError("type", type, f"Unknown analysis type: {type}")


# ═════════════════════════════════════════════════════════════════════════════
# PAGINATED READ
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def read_document(doc_id: str, limit: int = 5000, page_token: str = "") -> dict:
    """
    Read document content with pagination support.
    
    Args:
        doc_id: Document ID
        limit: Character limit for this read (default 5000)
        page_token: Start position for pagination
    
    Returns:
        dict with content, sections, next_page_token, has_more
    """
    cache_key = f"doc_content:{doc_id}"
    cached = _docs_cache.get(cache_key)
    
    if cached:
        full_content = cached
    else:
        docs = authenticate_docs()
        
        # Get document content
        result = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
        
        # Extract text content
        full_content = extract_text_from_document(result)
        _docs_cache.set(cache_key, full_content, ttl=300)
    
    # Handle pagination
    start_pos = int(page_token) if page_token and page_token.isdigit() else 0
    end_pos = start_pos + limit
    
    content_slice = full_content[start_pos:end_pos]
    has_more = end_pos < len(full_content)
    next_token = str(end_pos) if has_more else ""
    
    # Extract sections from this slice
    sections = extract_sections(content_slice)
    
    # Content intelligence
    intelligence = get_content_type_intelligence(full_content)
    
    # Update context
    _doc_context.add_viewed(doc_id)
    
    return {
        "id": doc_id,
        "content": content_slice,
        "sections": sections,
        "next_page_token": next_token,
        "has_more": has_more,
        "total_length": len(full_content),
        "current_position": start_pos,
        "intelligence": intelligence
    }


# ═════════════════════════════════════════════════════════════════════════════
# DOCUMENT OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_document_metadata(doc_id: str) -> dict:
    """Get document metadata."""
    docs = authenticate_docs()
    drive = authenticate_drive()
    
    # Get from Docs API
    doc = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
    
    # Get Drive metadata
    try:
        drive_meta = _docs_api_call(lambda: drive.files().get(
            fileId=doc_id,
            fields="id, name, modifiedTime, createdTime, owners, webViewLink"
        ).execute())
    except Exception:
        drive_meta = {}
    
    # Get content preview
    content_preview = extract_text_from_document(doc)[:200]
    
    # Standardize response
    response = standardize_doc_response({**doc, **drive_meta}, content_preview)
    response["revisionId"] = doc.get("revisionId", "")
    response["suggestionsViewMode"] = doc.get("suggestionsViewMode", "")
    
    # Update context
    _doc_context.add_viewed(doc_id)
    
    return response


@log_tool_call
def list_documents(limit: int = 20, page_token: str = "") -> dict:
    """List recent documents with pagination."""
    drive = authenticate_drive()
    
    query = "mimeType='application/vnd.google-apps.document' and trashed=false"
    
    params = {
        "q": query,
        "pageSize": min(limit, 50),
        "orderBy": "modifiedTime desc",
        "fields": "files(id, name, modifiedTime, createdTime, owners, webViewLink)"
    }
    if page_token:
        params["pageToken"] = page_token
    
    result = _docs_api_call(lambda: drive.files().list(**params).execute())
    
    files = result.get("files", [])
    docs = [standardize_doc_response(f) for f in files]
    
    # Update context
    _doc_context.add_search_results(docs)
    
    return paginated_response(
        items=docs,
        next_page_token=result.get("nextPageToken", ""),
        has_more=bool(result.get("nextPageToken")),
        total_count=result.get("resultSizeEstimate", len(docs))
    )


def _resolve_folder_id(folder_name: str) -> Optional[str]:
    """Resolve a folder name to its Drive folder ID."""
    drive = authenticate_drive()
    safe = sanitize_search_query(folder_name)
    results = _docs_api_call(lambda: drive.files().list(
        q=f"mimeType='application/vnd.google-apps.folder' and name contains '{safe}' and trashed=false",
        pageSize=1,
        fields="files(id, name)"
    ).execute())
    files = results.get("files", [])
    return files[0]["id"] if files else None


@log_tool_call
def search_documents(
    query: str = "",
    title: str = "",
    date_from: str = "",
    date_to: str = "",
    owner: str = "",
    folder_id: str = "",
    folder_name: str = "",
    recently_modified: bool = False,
    shared_with_me: bool = False,
    limit: int = 20,
    page_token: str = ""
) -> dict:
    """
    Search documents with advanced filters.

    Args:
        query: Full-text / title contains match
        title: Exact title-contains filter (alias for query)
        date_from: ISO date string — only docs modified after this date (e.g. "2024-01-01")
        date_to: ISO date string — only docs modified before this date
        owner: Filter by owner email or display name (Drive 'owners' contains)
        folder_id: Restrict search to this Drive folder ID
        folder_name: Restrict search to this folder name (resolved automatically)
        recently_modified: If True, sort by modifiedTime and return only last 7 days
        shared_with_me: If True, restrict to docs shared with the authenticated user
        limit: Max results (up to 50)
        page_token: Pagination token
    """
    drive = authenticate_drive()

    # Resolve folder name → ID
    resolved_folder = folder_id
    if not resolved_folder and folder_name:
        resolved_folder = _resolve_folder_id(folder_name)

    # Build compound query
    clauses = ["mimeType='application/vnd.google-apps.document'", "trashed=false"]

    search_term = title or query
    if search_term:
        safe = sanitize_search_query(search_term)
        clauses.append(f"name contains '{safe}'")

    if date_from:
        clauses.append(f"modifiedTime >= '{date_from}T00:00:00'")

    if date_to:
        clauses.append(f"modifiedTime <= '{date_to}T23:59:59'")

    if owner:
        safe_owner = sanitize_search_query(owner)
        clauses.append(f"'{safe_owner}' in owners")

    if resolved_folder:
        clauses.append(f"'{resolved_folder}' in parents")

    if recently_modified:
        import datetime as _dt
        cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        clauses.append(f"modifiedTime >= '{cutoff}'")

    if shared_with_me:
        clauses.append("sharedWithMe=true")

    drive_q = " and ".join(clauses)

    params = {
        "q": drive_q,
        "pageSize": min(limit, 50),
        "orderBy": "modifiedTime desc",
        "fields": "files(id, name, modifiedTime, createdTime, owners, webViewLink), nextPageToken, resultSizeEstimate"
    }
    if page_token:
        params["pageToken"] = page_token

    result = _docs_api_call(lambda: drive.files().list(**params).execute())

    files = result.get("files", [])
    docs = [standardize_doc_response(f) for f in files]

    _doc_context.add_search_results(docs)

    return paginated_response(
        items=docs,
        next_page_token=result.get("nextPageToken", ""),
        has_more=bool(result.get("nextPageToken")),
        total_count=result.get("resultSizeEstimate", len(docs))
    )


# ═════════════════════════════════════════════════════════════════════════════
# CONTENT MODIFICATION
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def append_content(doc_id: str, content: str) -> dict:
    """Append text to end of document."""
    docs = authenticate_docs()
    
    # Get current document to find end index
    doc = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
    end_index = get_document_end_index(doc)
    
    # Insert content
    requests = [{
        "insertText": {
            "location": {"index": end_index},
            "text": "\n" + content
        }
    }]
    
    result = _docs_api_call(lambda: docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute())
    
    # Invalidate cache
    invalidate_cache(f"doc_content:{doc_id}")
    
    return {
        "success": True,
        "doc_id": doc_id,
        "action": "append",
        "chars_added": len(content),
        "revisionId": result.get("revisionId", "")
    }


@log_tool_call
def insert_content(doc_id: str, content: str, index: int) -> dict:
    """Insert text at specific index."""
    docs = authenticate_docs()
    
    requests = [{
        "insertText": {
            "location": {"index": index},
            "text": content
        }
    }]
    
    result = _docs_api_call(lambda: docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute())
    
    invalidate_cache(f"doc_content:{doc_id}")
    
    return {
        "success": True,
        "doc_id": doc_id,
        "action": "insert",
        "index": index,
        "chars_added": len(content),
        "revisionId": result.get("revisionId", "")
    }


def _find_section_range(doc: dict, section_title: str) -> tuple[int, int, int]:
    """
    Find structural index range for a named section using Google Docs element indices.

    Returns:
        (heading_start, content_start, section_end)
        heading_start  — startIndex of the heading paragraph (-1 if not found)
        content_start  — endIndex of the heading paragraph (where body text begins)
        section_end    — startIndex of the next same/higher-level heading, or doc end
    """
    body = doc.get("body", {})
    elements = body.get("content", [])

    heading_start = -1
    content_start = -1
    section_level = 0

    for elem in elements:
        if "paragraph" not in elem:
            continue

        para = elem["paragraph"]
        style = para.get("paragraphStyle", {}).get("namedStyleType", "")
        text = "".join(
            e.get("textRun", {}).get("content", "")
            for e in para.get("elements", [])
        ).strip()

        if heading_start == -1:
            if "HEADING" in style and section_title.lower() in text.lower():
                heading_start = elem.get("startIndex", 0)
                content_start = elem.get("endIndex", heading_start)
                section_level = int(style[-1]) if style[-1].isdigit() else 1
        else:
            if "HEADING" in style:
                level = int(style[-1]) if style[-1].isdigit() else 1
                if level <= section_level:
                    return heading_start, content_start, elem.get("startIndex", content_start)

    if heading_start == -1:
        return -1, -1, -1

    # Section extends to end of document (endIndex of last element minus sentinel newline)
    doc_end = elements[-1].get("endIndex", content_start) - 1 if elements else content_start
    return heading_start, content_start, max(doc_end, content_start)


@log_tool_call
def replace_section(doc_id: str, section_title: str, new_content: str) -> dict:
    """Replace the body of a section identified by its heading title."""
    docs = authenticate_docs()

    doc = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
    heading_start, content_start, section_end = _find_section_range(doc, section_title)

    if heading_start == -1:
        raise DocNotFoundError(f"Section '{section_title}' not found in document")

    requests = []

    # Delete existing section body (preserve the heading itself)
    if content_start < section_end:
        requests.append({
            "deleteContentRange": {
                "range": {"startIndex": content_start, "endIndex": section_end}
            }
        })

    # Insert new content right after the heading
    requests.append({
        "insertText": {
            "location": {"index": content_start},
            "text": "\n" + new_content + "\n"
        }
    })

    result = _docs_api_call(lambda: docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute())

    invalidate_cache(f"doc_content:{doc_id}")

    return {
        "success": True,
        "doc_id": doc_id,
        "action": "replace_section",
        "section": section_title,
        "revisionId": result.get("revisionId", "")
    }


@log_tool_call
def delete_section(doc_id: str, section_title: str, require_confirmation: bool = True) -> dict:
    """Delete a section (heading + body) from a document."""
    if require_confirmation:
        raise DocSafetyError(
            f"Deleting section '{section_title}' requires confirmation. "
            "Set require_confirmation=False to proceed."
        )

    docs = authenticate_docs()

    doc = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
    heading_start, _content_start, section_end = _find_section_range(doc, section_title)

    if heading_start == -1:
        raise DocNotFoundError(f"Section '{section_title}' not found in document")

    if heading_start >= section_end:
        raise DocValidationError("section", section_title, "section range is empty")

    requests = [{
        "deleteContentRange": {
            "range": {"startIndex": heading_start, "endIndex": section_end}
        }
    }]

    result = _docs_api_call(lambda: docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute())

    invalidate_cache(f"doc_content:{doc_id}")

    return {
        "success": True,
        "doc_id": doc_id,
        "action": "delete_section",
        "section": section_title,
        "chars_removed": section_end - heading_start,
        "revisionId": result.get("revisionId", "")
    }


@log_tool_call
def append_multiple_sections(doc_id: str, sections: list) -> dict:
    """
    Append multiple sections to a document in a single API call.

    Args:
        doc_id: Target document ID
        sections: List of {"heading": str, "content": str, "heading_level": int (optional, default 2)}

    Returns:
        dict with success, sections_added, chars_added
    """
    if not sections:
        raise DocValidationError("sections", sections, "sections list cannot be empty")
    if len(sections) > BATCH_SECTION_LIMIT:
        raise DocValidationError("sections", len(sections), f"Max {BATCH_SECTION_LIMIT} sections per batch")

    docs = authenticate_docs()
    doc = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
    end_index = get_document_end_index(doc)

    # Build full text block once so we issue a single insertText
    text_block = ""
    for sec in sections:
        heading = sec.get("heading", "")
        content = sec.get("content", "")
        if heading:
            text_block += f"\n{heading}\n"
        if content:
            text_block += content + "\n"

    requests = [{
        "insertText": {
            "location": {"index": end_index},
            "text": text_block
        }
    }]

    result = _docs_api_call(lambda: docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute())

    invalidate_cache(f"doc_content:{doc_id}")

    return {
        "success": True,
        "doc_id": doc_id,
        "action": "append_multiple_sections",
        "sections_added": len(sections),
        "chars_added": len(text_block),
        "revisionId": result.get("revisionId", "")
    }


@log_tool_call
def replace_multiple_sections(
    doc_id: str,
    sections: list,
    require_confirmation: bool = False
) -> dict:
    """
    Replace multiple named sections sequentially with partial-failure handling.

    Args:
        doc_id: Target document ID
        sections: List of {"section": str, "content": str}
        require_confirmation: Raise DocSafetyError for large content replacements

    Returns:
        dict with succeeded, failed lists
    """
    if not sections:
        raise DocValidationError("sections", sections, "sections list cannot be empty")
    if len(sections) > BATCH_REPLACE_LIMIT:
        raise DocValidationError("sections", len(sections), f"Max {BATCH_REPLACE_LIMIT} sections per batch")

    succeeded = []
    failed = []

    for item in sections:
        section_title = item.get("section", "")
        new_content = item.get("content", "")

        if not section_title:
            failed.append({"section": section_title, "error": "section name required"})
            continue

        # Safety check per section
        needs_confirm, warning = check_replace_safety(new_content, require_confirmation=require_confirmation)
        if needs_confirm:
            failed.append({"section": section_title, "error": f"Safety check: {warning}"})
            continue

        try:
            replace_section(doc_id=doc_id, section_title=section_title, new_content=new_content)
            succeeded.append(section_title)
        except Exception as exc:
            failed.append({"section": section_title, "error": str(exc)})

    return {
        "success": len(failed) == 0,
        "doc_id": doc_id,
        "action": "replace_multiple_sections",
        "succeeded": succeeded,
        "failed": failed,
        "total_requested": len(sections)
    }


@log_tool_call
def delete_multiple_sections(
    doc_id: str,
    section_titles: list,
    require_confirmation: bool = True
) -> dict:
    """
    Delete multiple named sections with batch safety gate.

    Args:
        doc_id: Target document ID
        section_titles: List of section heading strings to delete
        require_confirmation: Requires explicit False for multi-section deletes

    Returns:
        dict with succeeded, failed lists
    """
    if not section_titles:
        raise DocValidationError("section_titles", section_titles, "section_titles cannot be empty")

    needs_confirm, warning = check_delete_batch_safety(len(section_titles), require_confirmation)
    if needs_confirm:
        raise DocSafetyError(warning)

    succeeded = []
    failed = []

    for title in section_titles:
        try:
            delete_section(doc_id=doc_id, section_title=title, require_confirmation=False)
            succeeded.append(title)
        except Exception as exc:
            failed.append({"section": title, "error": str(exc)})

    return {
        "success": len(failed) == 0,
        "doc_id": doc_id,
        "action": "delete_multiple_sections",
        "succeeded": succeeded,
        "failed": failed,
        "total_requested": len(section_titles)
    }


@log_tool_call
def clear_document(doc_id: str, require_confirmation: bool = True) -> dict:
    """Clear all content from document."""
    if require_confirmation:
        raise DocSafetyError(
            "Clearing document requires confirmation. "
            "Set require_confirmation=False to proceed."
        )
    
    docs = authenticate_docs()
    
    # Get document to find content range
    doc = _docs_api_call(lambda: docs.documents().get(documentId=doc_id).execute())
    end_index = get_document_end_index(doc)
    
    if end_index <= 2:  # Only title or empty
        return {
            "success": True,
            "doc_id": doc_id,
            "action": "clear",
            "message": "Document already empty"
        }
    
    # Delete content after title (index 1)
    requests = [{
        "deleteContentRange": {
            "range": {
                "startIndex": 2,
                "endIndex": end_index - 1
            }
        }
    }]
    
    result = _docs_api_call(lambda: docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests}
    ).execute())
    
    invalidate_cache(f"doc_content:{doc_id}")
    
    return {
        "success": True,
        "doc_id": doc_id,
        "action": "clear",
        "chars_removed": end_index - 2,
        "revisionId": result.get("revisionId", "")
    }


# ═════════════════════════════════════════════════════════════════════════════
# DOCUMENT CREATION
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def create_blank_document(title: str, content: str = "") -> dict:
    """Create blank document with optional content."""
    docs = authenticate_docs()

    doc = {
        "title": title
    }
    
    result = _docs_api_call(lambda: docs.documents().create(body=doc).execute())
    doc_id = result.get("documentId")
    
    # Add content if provided
    if content:
        append_content(doc_id, content)
    
    # Get full metadata
    return get_document_metadata(doc_id)


@log_tool_call
def create_from_template(title: str, template_id: str = "", template_name: str = "") -> dict:
    """Create document from template."""
    drive = authenticate_drive()

    # If template_name provided, resolve to ID via metadata
    if template_name and not template_id:
        meta = find_template_by_name(template_name)
        if not meta:
            raise DocNotFoundError(f"Template '{template_name}' not found")
        template_id = meta["id"]

    if not template_id:
        return create_blank_document(title)

    result = _docs_api_call(lambda: drive.files().copy(
        fileId=template_id,
        body={"name": title}
    ).execute())

    doc_id = result.get("id")
    return get_document_metadata(doc_id)


def find_template_by_name(name: str) -> Optional[dict]:
    """
    Find a template by name. Returns metadata dict {id, title, modified, url} or None.
    Tries exact match first, then partial match.
    """
    drive = authenticate_drive()
    safe = sanitize_search_query(name)

    # Exact match first
    exact_q = f"mimeType='application/vnd.google-apps.document' and name='{safe}' and trashed=false"
    results = _docs_api_call(lambda: drive.files().list(
        q=exact_q,
        pageSize=5,
        fields="files(id, name, modifiedTime, webViewLink)"
    ).execute())

    files = results.get("files", [])

    # Fallback: partial match
    if not files:
        partial_q = f"mimeType='application/vnd.google-apps.document' and name contains '{safe}' and trashed=false"
        results = _docs_api_call(lambda: drive.files().list(
            q=partial_q,
            pageSize=5,
            fields="files(id, name, modifiedTime, webViewLink)"
        ).execute())
        files = results.get("files", [])

    if not files:
        return None

    f = files[0]
    return {
        "id": f["id"],
        "title": f.get("name", ""),
        "modified": f.get("modifiedTime", ""),
        "url": f.get("webViewLink", f"https://docs.google.com/document/d/{f['id']}/edit")
    }


# ═════════════════════════════════════════════════════════════════════════════
# ANALYSIS FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def _llm_analyze(content: str, analysis_type: str) -> Optional[dict]:
    """
    Run LLM-powered analysis. Returns parsed dict on success, None on failure.
    analysis_type: "summary" | "structure" | "key_points" | "action_items"
    """
    import json, re as _re

    prompts = {
        "summary": (
            "Summarize the following document in 3–5 sentences. "
            "Return JSON: {\"summary\": \"...\", \"topics\": [\"...\"]}\n\nDocument:\n"
        ),
        "structure": (
            "Analyze the structure of this document. "
            "Return JSON: {\"outline\": [{\"level\": 1, \"title\": \"...\"}], \"section_count\": N, \"doc_type\": \"...\"}\n\nDocument:\n"
        ),
        "key_points": (
            "Extract the top key points (max 10) from this document. "
            "Return JSON: {\"key_points\": [\"...\"]}\n\nDocument:\n"
        ),
        "action_items": (
            "Extract all action items, tasks, and to-dos from this document. "
            "Return JSON: {\"action_items\": [{\"text\": \"...\", \"assignee\": \"\" , \"due\": \"\"}]}\n\nDocument:\n"
        ),
    }

    prompt = prompts.get(analysis_type)
    if not prompt:
        return None

    # Truncate to avoid token overflow
    truncated = content[:LARGE_CONTENT_THRESHOLD]
    messages = [
        {"role": "system", "content": "You are a document analysis assistant. Always respond with valid JSON only."},
        {"role": "user", "content": prompt + truncated}
    ]

    try:
        raw = call_model(messages)
        # Extract JSON block
        match = _re.search(r'\{[\s\S]*\}', raw)
        if match:
            return json.loads(match.group(0))
    except Exception:
        pass

    return None


def analyze_summary(content: str) -> dict:
    """Generate document summary (LLM-first, rule-based fallback)."""
    llm_result = _llm_analyze(content, "summary")
    if llm_result and "summary" in llm_result:
        return {
            "analysis_type": "summary",
            "summary": llm_result["summary"],
            "topics": llm_result.get("topics", []),
            "source": "llm",
            "length": len(content)
        }

    # Fallback: extractive summary
    paragraphs = [p for p in content.split('\n\n') if p.strip()]
    summary_sentences = []
    for p in paragraphs[:3]:
        sentences = p.split('. ')
        if sentences:
            summary_sentences.append(sentences[0] + '.')

    return {
        "analysis_type": "summary",
        "summary": ' '.join(summary_sentences),
        "topics": [],
        "source": "rule_based",
        "length": len(content)
    }


def analyze_structure(content: str) -> dict:
    """Analyze document structure (LLM-first, rule-based fallback)."""
    llm_result = _llm_analyze(content, "structure")
    if llm_result and "outline" in llm_result:
        return {
            "analysis_type": "structure",
            "outline": llm_result["outline"],
            "section_count": llm_result.get("section_count", len(llm_result["outline"])),
            "doc_type": llm_result.get("doc_type", "document"),
            "source": "llm"
        }

    # Fallback: regex-based
    sections = extract_sections(content)
    headings = [s for s in sections if s["type"] == "heading"]
    bullets = [s for s in sections if s["type"] == "bullet"]

    return {
        "analysis_type": "structure",
        "outline": [{"level": h["level"], "title": h["title"]} for h in headings],
        "section_count": len(headings),
        "bullet_count": len(bullets),
        "doc_type": "structured-document" if headings else "document",
        "source": "rule_based"
    }


def analyze_key_points(content: str) -> dict:
    """Extract key points (LLM-first, rule-based fallback)."""
    llm_result = _llm_analyze(content, "key_points")
    if llm_result and "key_points" in llm_result:
        return {
            "analysis_type": "key_points",
            "key_points": llm_result["key_points"][:10],
            "count": len(llm_result["key_points"][:10]),
            "source": "llm"
        }

    # Fallback: pattern-based
    lines = content.split('\n')
    key_points = []
    for line in lines:
        if line.strip().startswith(('- ', '* ', '• ')):
            key_points.append(line.strip('- *•').strip())
        elif line.strip().startswith(('1. ', '2. ', '3. ')):
            key_points.append(line.strip('123456789. ').strip())
        elif '**' in line or '__' in line:
            key_points.append(line.strip('*_').strip())

    key_points = key_points[:10]
    return {
        "analysis_type": "key_points",
        "key_points": key_points,
        "count": len(key_points),
        "source": "rule_based"
    }


def analyze_action_items(content: str) -> dict:
    """Extract action items (LLM-first, rule-based fallback)."""
    llm_result = _llm_analyze(content, "action_items")
    if llm_result and "action_items" in llm_result:
        items = llm_result["action_items"]
        return {
            "analysis_type": "action_items",
            "action_items": items,
            "count": len(items),
            "source": "llm"
        }

    # Fallback: keyword-based
    lines = content.split('\n')
    action_keywords = ['action', 'todo', 'task', 'follow up', 'follow-up', 'due', 'deadline', 'assign']
    action_items = []

    for line in lines:
        line_lower = line.lower()
        if any(kw in line_lower for kw in action_keywords):
            action_items.append({"text": line.strip('- *•').strip(), "assignee": "", "due": ""})
        elif '[ ]' in line or '[x]' in line or '☐' in line:
            action_items.append({"text": line.strip(), "assignee": "", "due": ""})

    return {
        "analysis_type": "action_items",
        "action_items": action_items,
        "count": len(action_items),
        "source": "rule_based"
    }


def analyze_word_count(content: str) -> dict:
    """Get document statistics."""
    words = content.split()
    lines = content.split('\n')
    characters = len(content)
    characters_no_spaces = len(content.replace(' ', '').replace('\n', ''))
    
    return {
        "analysis_type": "word_count",
        "words": len(words),
        "lines": len(lines),
        "characters": characters,
        "characters_no_spaces": characters_no_spaces,
        "average_word_length": round(characters_no_spaces / len(words), 2) if words else 0
    }


# ═════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def extract_text_from_document(doc: dict) -> str:
    """Extract plain text from document structure."""
    content = []
    
    def extract_elements(elements):
        for elem in elements:
            if 'paragraph' in elem:
                para = elem['paragraph']
                para_text = ""
                for element in para.get('elements', []):
                    if 'textRun' in element:
                        para_text += element['textRun'].get('content', '')
                if para_text:
                    content.append(para_text.rstrip())
            elif 'table' in elem:
                # Simple table extraction
                content.append("[Table]")
            elif 'sectionBreak' in elem:
                pass  # Skip section breaks
    
    body = doc.get('body', {})
    content_elements = body.get('content', [])
    extract_elements(content_elements)
    
    return '\n'.join(content)


def get_document_end_index(doc: dict) -> int:
    """Get the end index for appending content."""
    body = doc.get('body', {})
    content = body.get('content', [])
    
    if not content:
        return 1  # Start after title
    
    # Find last element
    last_elem = content[-1]
    if 'paragraph' in last_elem:
        elements = last_elem['paragraph'].get('elements', [])
        if elements:
            return elements[-1].get('endIndex', 1)
    
    return 1


# ═════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY (v1 tool wrappers)
# ═════════════════════════════════════════════════════════════════════════════

def get_doc_by_id(doc_id: str) -> dict:
    """Backward compatible wrapper."""
    return get_document_metadata(doc_id)


def create_doc(title: str) -> dict:
    """Backward compatible wrapper."""
    return create_blank_document(title)


def update_doc(doc_id: str, content: str) -> dict:
    """Backward compatible wrapper."""
    return append_content(doc_id, content)


# ═════════════════════════════════════════════════════════════════════════════
# STATISTICS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_docs_context_summary() -> dict:
    """
    Return structured context summary: current document, recent search results,
    and total tracked IDs — useful for injecting into LLM system prompts.
    """
    return _doc_context.get_context_summary()


@log_tool_call
def get_docs_stats() -> dict:
    """Get Docs MCP usage statistics including per-tool breakdown and recent errors."""
    return {
        "logger_stats": _docs_logger.get_stats(),
        "tool_stats": _docs_logger.get_tool_stats(),
        "recent_errors": _docs_logger.get_recent_errors(n=5),
        "cache_stats": _docs_cache.stats(),
        "context": {
            "last_viewed_count": len(_doc_context.last_viewed_ids),
            "last_search_count": len(_doc_context.last_search_results),
            "current_doc": _doc_context.current_doc_id
        }
    }


@log_tool_call
def clear_docs_cache(prefix: str = "") -> dict:
    """Clear Docs cache."""
    return invalidate_cache(prefix)
