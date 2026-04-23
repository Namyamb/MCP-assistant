"""
Google Sheets MCP v2 — Agent-Native Spreadsheet Integration

Production-grade Sheets tools with:
- Natural language ID / tab / range resolution
- Unified action tools (sheet_action, sheet_modify, sheet_analyze, sheet_structure)
- Pagination for large sheets (client-side row slicing)
- Batch operations (multi-range update, bulk append/delete)
- Formula support with safety validation
- Data analysis layer (stats, missing values, outliers, column summaries)
- TTL caching with prefix-based invalidation
- Typed exception hierarchy
- Per-tool structured logging
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from app.core.config import TOKEN_FILE, ALL_SCOPES
from app.core.llm_client import call_model

from app.integrations.sheets.utils import (
    SheetError, SheetNotFoundError, SheetAmbiguityError, SheetPermissionError,
    SheetRateLimitError, SheetValidationError, SheetRangeError, SheetSafetyError,
    _sheets_cache, invalidate_cache, cached,
    _sheets_logger, log_tool_call,
    _sheet_context,
    validate_sheet_title, sanitize_search_query, extract_sheet_id_from_url,
    standardize_sheet_response, paginated_response,
    parse_natural_range, col_letter_to_index, col_index_to_letter,
    detect_headers, get_column_index, compute_column_stats,
    detect_outliers_in_column, detect_data_range,
    check_write_safety, check_delete_safety, check_formula_safety,
    MAX_ROWS_PER_READ, LARGE_DATA_THRESHOLD, BATCH_ROW_LIMIT, BATCH_RANGE_LIMIT,
)

_thread_local = threading.local()


# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═════════════════════════════════════════════════════════════════════════════

def _load_creds() -> Optional[Credentials]:
    if not TOKEN_FILE.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), ALL_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            TOKEN_FILE.unlink(missing_ok=True)
            return None
    return creds if (creds and creds.valid) else None


def _reset_services() -> None:
    _thread_local.sheets_service = None
    _thread_local.drive_service = None


def authenticate_sheets():
    """Return thread-local Sheets API service."""
    if getattr(_thread_local, "sheets_service", None) is not None:
        return _thread_local.sheets_service
    creds = _load_creds()
    if not creds:
        raise PermissionError(
            "Google Sheets not authenticated. Run `python auth.py` then restart."
        )
    _thread_local.sheets_service = build("sheets", "v4", credentials=creds)
    return _thread_local.sheets_service


def authenticate_drive():
    """Return thread-local Drive API service."""
    if getattr(_thread_local, "drive_service", None) is not None:
        return _thread_local.drive_service
    creds = _load_creds()
    if not creds:
        raise PermissionError(
            "Google Drive not authenticated. Run `python auth.py` then restart."
        )
    _thread_local.drive_service = build("drive", "v3", credentials=creds)
    return _thread_local.drive_service


def _api_call(fn, retries: int = 3, backoff: float = 1.5):
    """Execute API call with retry + typed error translation."""
    last_err = None
    for attempt in range(retries):
        try:
            return fn()
        except HttpError as exc:
            status = exc.resp.status if hasattr(exc, "resp") else 0
            if status == 401:
                _reset_services()
                last_err = exc
                continue
            if status == 404:
                raise SheetNotFoundError(f"Resource not found: {exc}")
            if status == 403:
                raise SheetPermissionError(f"Permission denied: {exc}")
            if status == 429:
                retry_after = int(exc.resp.headers.get("Retry-After", 60))
                raise SheetRateLimitError("Rate limit exceeded", retry_after=retry_after)
            if status in (500, 502, 503, 504):
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            raise SheetError(f"Sheets API error ({status}): {exc}")
        except (OSError, ConnectionError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise SheetError(f"API call failed after {retries} attempts: {last_err}")


# ═════════════════════════════════════════════════════════════════════════════
# ID / TAB / RANGE RESOLUTION
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def resolve_sheet_id(reference: str = "", use_context: bool = True,
                     use_drive_fallback: bool = True) -> str:
    """
    Resolve natural language reference → spreadsheet ID.

    Supports:
    - "latest", "last", "most recent" → most recently viewed
    - "this sheet", "that sheet", "current" → active sheet
    - "first", "second" → from last search results
    - "sheet named sales data" → Drive search by title
    - Raw Google Sheets URL → extracted ID
    - Raw ID (44-char alphanumeric) → passthrough
    """
    # URL extraction
    if reference.startswith("https://"):
        sid = extract_sheet_id_from_url(reference)
        if sid:
            return sid

    # Raw ID: no spaces, 25-50 chars, alphanumeric/dashes
    if reference and not any(c.isspace() for c in reference):
        if 25 <= len(reference) <= 50 and reference.replace("-", "").replace("_", "").isalnum():
            return reference

    # Context resolution
    if use_context:
        resolved = _sheet_context.resolve_reference(reference or "latest")
        if resolved:
            return resolved

    # Drive fallback — search by title
    if use_drive_fallback:
        drive = authenticate_drive()

        name = reference
        for prefix in ("sheet named ", "spreadsheet named ", "file named "):
            if reference.lower().startswith(prefix):
                name = reference[len(prefix):].strip()
                break

        safe = sanitize_search_query(name)
        q = (f"mimeType='application/vnd.google-apps.spreadsheet' "
             f"and name contains '{safe}' and trashed=false")

        results = _api_call(lambda: drive.files().list(
            q=q, pageSize=5,
            fields="files(id, name, modifiedTime)"
        ).execute())

        files = results.get("files", [])
        if files:
            if len(files) == 1:
                return files[0]["id"]
            raise SheetAmbiguityError(
                f"Multiple spreadsheets match '{reference}'",
                matches=[{"id": f["id"], "title": f["name"]} for f in files],
            )

    raise SheetNotFoundError(f"Could not resolve spreadsheet reference: '{reference}'")


def resolve_tab_name(sheet_id: str, reference: str) -> str:
    """
    Resolve natural language tab reference → actual tab name.

    Supports:
    - Exact tab name passthrough: "Sheet1", "Sales Data"
    - Ordinals: "first tab", "second tab", "tab 1"
    - "last tab" → final tab
    - Partial name match: "sales" → "Sales Data"
    - Current context tab
    """
    ref = reference.strip()
    ref_lower = ref.lower()

    # Get all tabs for this sheet
    service = authenticate_sheets()
    meta = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties(title,index)"
    ).execute())
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if not tabs:
        raise SheetNotFoundError(f"Spreadsheet {sheet_id} has no tabs")

    # Exact match
    for t in tabs:
        if t.lower() == ref_lower:
            return t

    # Context tab
    if ref_lower in ("this tab", "current tab", "active tab", "current"):
        return _sheet_context.current_tab or tabs[0]

    # "first/last tab" and ordinals
    ordinals = {
        "first": 0, "1st": 0, "second": 1, "2nd": 1,
        "third": 2, "3rd": 2, "fourth": 3, "4th": 3,
        "fifth": 4, "5th": 4,
    }
    for kw, idx in ordinals.items():
        if ref_lower in (kw, f"{kw} tab", f"tab {idx + 1}"):
            if idx < len(tabs):
                return tabs[idx]

    if ref_lower in ("last tab", "last sheet", "final tab"):
        return tabs[-1]

    # Partial match
    for t in tabs:
        if ref_lower in t.lower():
            return t

    raise SheetNotFoundError(f"Tab '{reference}' not found in spreadsheet. Available: {tabs}")


def resolve_range(
    reference: str,
    sheet_id: str = "",
    tab: str = "",
    headers: list[str] = None,
    row_count: int = None,
    col_count: int = None,
) -> str:
    """
    Resolve any range reference (A1 notation or natural language) → A1 notation.
    If tab is specified, prefixes the result with "Tab!".
    """
    parsed = parse_natural_range(
        reference,
        headers=headers or _sheet_context.last_read_headers,
        row_count=row_count,
        col_count=col_count,
    )
    if tab and "!" not in parsed:
        return f"{tab}!{parsed}"
    return parsed


# ═════════════════════════════════════════════════════════════════════════════
# UNIFIED TOOLS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def sheet_action(
    action: str = "read",
    sheet_id: str = "",
    sheet_reference: str = "",
    query: str = "",
    tab: str = "",
    range_name: str = "",
    limit: int = 100,
    page_token: str = "",
    include_metadata: bool = False,
) -> dict:
    """
    UNIFIED: Read, list, search, or get metadata for spreadsheets.

    Actions:
      read         — read data from a sheet/tab/range (paginated)
      list         — list recent spreadsheets
      search       — search spreadsheets by title
      get_metadata — get spreadsheet properties and tab list

    Args:
        action: one of "read", "list", "search", "get_metadata"
        sheet_id: direct spreadsheet ID
        sheet_reference: natural language reference
        query: search query (for search action)
        tab: tab/sheet name (for read)
        range_name: A1 or natural language range (for read)
        limit: max rows per page (for read) or max results (for list/search)
        page_token: pagination cursor
        include_metadata: if True, attach tab list to read response
    """
    if action == "read":
        if not sheet_id and sheet_reference:
            sheet_id = resolve_sheet_id(sheet_reference)
        elif not sheet_id:
            raise SheetValidationError("sheet_id", "", "Either sheet_id or sheet_reference required")
        return read_sheet_data(sheet_id, tab=tab, range_name=range_name,
                               limit=limit, page_token=page_token,
                               include_metadata=include_metadata)

    elif action == "list":
        return list_spreadsheets(limit=limit, page_token=page_token)

    elif action == "search":
        if not query:
            raise SheetValidationError("query", "", "query is required for search action")
        return search_spreadsheets(query=query, limit=limit, page_token=page_token)

    elif action == "get_metadata":
        if not sheet_id and sheet_reference:
            sheet_id = resolve_sheet_id(sheet_reference)
        elif not sheet_id:
            raise SheetValidationError("sheet_id", "", "Either sheet_id or sheet_reference required")
        return get_sheet_metadata(sheet_id)

    else:
        raise SheetValidationError("action", action,
                                   "Unknown action. Use: read, list, search, get_metadata")


@log_tool_call
def sheet_modify(
    action: str = "append",
    sheet_id: str = "",
    sheet_reference: str = "",
    tab: str = "",
    range_name: str = "",
    values: list = None,
    formula: str = "",
    target_cell: str = "",
    row_indices: list = None,
    require_confirmation: bool = False,
) -> dict:
    """
    UNIFIED: Write, update, append, clear, delete rows, or insert formulas.

    Actions:
      write         — overwrite a range with values (2-D list)
      append        — append rows after last row with data
      update_cell   — write a single cell value or formula
      clear         — clear a range
      delete_rows   — delete specific rows by 1-based index
      insert_formula— insert a formula into a cell

    Args:
        action: one of "write", "append", "update_cell", "clear", "delete_rows", "insert_formula"
        sheet_id / sheet_reference: target spreadsheet
        tab: tab name (defaults to first tab)
        range_name: A1 notation or natural language range
        values: 2-D list of values for write/append
        formula: formula string starting with "=" (for insert_formula)
        target_cell: cell address like "B1" (for update_cell / insert_formula)
        row_indices: list of 1-based row numbers to delete
        require_confirmation: raise SheetSafetyError for large destructive ops
    """
    if not sheet_id and sheet_reference:
        sheet_id = resolve_sheet_id(sheet_reference)
    elif not sheet_id:
        raise SheetValidationError("sheet_id", "", "Either sheet_id or sheet_reference required")

    if action == "write":
        if not values:
            raise SheetValidationError("values", values, "values required for write action")
        rng = resolve_range(range_name or "A1", sheet_id=sheet_id, tab=tab)
        return write_range(sheet_id, range_name=rng, values=values,
                           require_confirmation=require_confirmation)

    elif action == "append":
        if not values:
            raise SheetValidationError("values", values, "values required for append action")
        rng = resolve_range(range_name or tab or "Sheet1", sheet_id=sheet_id, tab=tab)
        return append_rows(sheet_id, range_name=rng, values=values)

    elif action == "update_cell":
        if not target_cell:
            raise SheetValidationError("target_cell", target_cell, "target_cell required for update_cell")
        cell_ref = f"{tab}!{target_cell}" if tab else target_cell
        cell_value = formula if formula else (values[0][0] if values and values[0] else "")
        return update_cell(sheet_id, cell=cell_ref, value=cell_value)

    elif action == "clear":
        rng = resolve_range(range_name or "A:ZZ", sheet_id=sheet_id, tab=tab)
        return clear_range(sheet_id, range_name=rng, require_confirmation=require_confirmation)

    elif action == "delete_rows":
        if not row_indices:
            raise SheetValidationError("row_indices", row_indices, "row_indices required for delete_rows")
        return delete_rows_bulk(sheet_id, tab=tab, row_indices=row_indices,
                                require_confirmation=require_confirmation)

    elif action == "insert_formula":
        if not formula:
            raise SheetValidationError("formula", formula, "formula required for insert_formula")
        if not target_cell:
            raise SheetValidationError("target_cell", target_cell, "target_cell required for insert_formula")
        cell_ref = f"{tab}!{target_cell}" if tab else target_cell
        return insert_formula(sheet_id, cell=cell_ref, formula=formula)

    else:
        raise SheetValidationError("action", action,
                                   "Unknown action. Use: write, append, update_cell, clear, delete_rows, insert_formula")


@log_tool_call
def sheet_analyze(
    type: str = "summary",
    sheet_id: str = "",
    sheet_reference: str = "",
    tab: str = "",
    range_name: str = "",
    column: str = "",
) -> dict:
    """
    UNIFIED: Analyze spreadsheet data.

    Types:
      summary       — row/column counts, headers, data types, empty cells
      column_stats  — min/max/mean/median/stdev for a specific column
      missing_values— identify cells/rows with empty data
      outliers      — detect numeric outliers using IQR in a column

    Args:
        type: one of "summary", "column_stats", "missing_values", "outliers"
        sheet_id / sheet_reference: target spreadsheet
        tab: tab name (defaults to first tab)
        range_name: A1 or natural language range
        column: column letter, name, or index (for column_stats / outliers)
    """
    if not sheet_id and sheet_reference:
        sheet_id = resolve_sheet_id(sheet_reference)
    elif not sheet_id:
        raise SheetValidationError("sheet_id", "", "Either sheet_id or sheet_reference required")

    # Load raw data for the analysis
    raw = read_sheet_data(sheet_id, tab=tab, range_name=range_name, limit=MAX_ROWS_PER_READ)
    rows = raw.get("rows", [])
    headers = raw.get("headers", [])

    if type == "summary":
        return analyze_sheet_summary(sheet_id, rows=rows, headers=headers, raw_meta=raw)

    elif type == "column_stats":
        return analyze_column_stats(rows=rows, headers=headers, column=column)

    elif type == "missing_values":
        return detect_missing_values(rows=rows, headers=headers)

    elif type == "outliers":
        return detect_outliers(rows=rows, headers=headers, column=column)

    else:
        raise SheetValidationError("type", type,
                                   "Unknown type. Use: summary, column_stats, missing_values, outliers")


@log_tool_call
def sheet_structure(
    action: str = "get_tabs",
    sheet_id: str = "",
    sheet_reference: str = "",
    tab_name: str = "",
    new_name: str = "",
    require_confirmation: bool = True,
) -> dict:
    """
    UNIFIED: Manage spreadsheet tabs.

    Actions:
      get_tabs    — list all tabs with row/col counts
      add_tab     — add a new tab
      rename_tab  — rename an existing tab
      delete_tab  — delete a tab (requires confirmation)
    """
    if not sheet_id and sheet_reference:
        sheet_id = resolve_sheet_id(sheet_reference)
    elif not sheet_id:
        raise SheetValidationError("sheet_id", "", "Either sheet_id or sheet_reference required")

    if action == "get_tabs":
        meta = get_sheet_metadata(sheet_id)
        return {"sheet_id": sheet_id, "tabs": meta.get("tabs", [])}

    elif action == "add_tab":
        if not tab_name:
            raise SheetValidationError("tab_name", tab_name, "tab_name required for add_tab")
        return add_tab(sheet_id, tab_name=tab_name)

    elif action == "rename_tab":
        if not tab_name:
            raise SheetValidationError("tab_name", tab_name, "tab_name required for rename_tab")
        if not new_name:
            raise SheetValidationError("new_name", new_name, "new_name required for rename_tab")
        return rename_tab(sheet_id, old_name=tab_name, new_name=new_name)

    elif action == "delete_tab":
        if not tab_name:
            raise SheetValidationError("tab_name", tab_name, "tab_name required for delete_tab")
        return delete_tab(sheet_id, tab_name=tab_name, require_confirmation=require_confirmation)

    else:
        raise SheetValidationError("action", action,
                                   "Unknown action. Use: get_tabs, add_tab, rename_tab, delete_tab")


# ═════════════════════════════════════════════════════════════════════════════
# METADATA & SEARCH
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_sheet_metadata(sheet_id: str) -> dict:
    """Get spreadsheet properties, all tab names, and row/column counts."""
    cache_key = f"meta:{sheet_id}"
    cached_val = _sheets_cache.get(cache_key)
    if cached_val:
        return cached_val

    service = authenticate_sheets()
    drive = authenticate_drive()

    result = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="spreadsheetId,properties,sheets.properties",
    ).execute())

    tabs = [
        {
            "name": s["properties"]["title"],
            "index": s["properties"].get("index", 0),
            "sheet_id": s["properties"].get("sheetId", 0),
            "rows": s["properties"].get("gridProperties", {}).get("rowCount", 0),
            "cols": s["properties"].get("gridProperties", {}).get("columnCount", 0),
        }
        for s in result.get("sheets", [])
    ]

    try:
        drive_meta = _api_call(lambda: drive.files().get(
            fileId=sheet_id,
            fields="id,name,modifiedTime,createdTime,owners,webViewLink"
        ).execute())
    except Exception:
        drive_meta = {}

    response = {
        "id": sheet_id,
        "title": result.get("properties", {}).get("title", "Untitled"),
        "url": drive_meta.get("webViewLink", f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"),
        "created": drive_meta.get("createdTime", ""),
        "modified": drive_meta.get("modifiedTime", ""),
        "owner": (drive_meta.get("owners") or [{}])[0].get("displayName", "Unknown"),
        "tabs": tabs,
        "tab_count": len(tabs),
    }

    _sheets_cache.set(cache_key, response, ttl=300)
    _sheet_context.add_viewed(sheet_id)
    return response


@log_tool_call
def list_spreadsheets(limit: int = 20, page_token: str = "") -> dict:
    """List recent spreadsheets from Drive."""
    drive = authenticate_drive()

    params = {
        "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
        "pageSize": min(limit, 50),
        "orderBy": "modifiedTime desc",
        "fields": "files(id,name,modifiedTime,createdTime,owners,webViewLink),nextPageToken,resultSizeEstimate",
    }
    if page_token:
        params["pageToken"] = page_token

    result = _api_call(lambda: drive.files().list(**params).execute())
    files = result.get("files", [])
    sheets = [standardize_sheet_response(f) for f in files]

    _sheet_context.add_search_results(sheets)

    return paginated_response(
        items=sheets,
        next_page_token=result.get("nextPageToken", ""),
        has_more=bool(result.get("nextPageToken")),
        total_count=result.get("resultSizeEstimate", len(sheets)),
    )


@log_tool_call
def search_spreadsheets(
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    owner: str = "",
    recently_modified: bool = False,
    limit: int = 20,
    page_token: str = "",
) -> dict:
    """
    Search spreadsheets in Drive with optional filters.

    Args:
        query: Title contains this string
        date_from / date_to: ISO date range filter
        owner: Filter by owner email
        recently_modified: Restrict to last 7 days
        limit / page_token: Pagination
    """
    drive = authenticate_drive()

    clauses = [
        "mimeType='application/vnd.google-apps.spreadsheet'",
        "trashed=false",
    ]

    if query:
        safe = sanitize_search_query(query)
        clauses.append(f"name contains '{safe}'")

    if date_from:
        clauses.append(f"modifiedTime >= '{date_from}T00:00:00'")

    if date_to:
        clauses.append(f"modifiedTime <= '{date_to}T23:59:59'")

    if owner:
        clauses.append(f"'{sanitize_search_query(owner)}' in owners")

    if recently_modified:
        import datetime as _dt
        cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        clauses.append(f"modifiedTime >= '{cutoff}'")

    params = {
        "q": " and ".join(clauses),
        "pageSize": min(limit, 50),
        "orderBy": "modifiedTime desc",
        "fields": "files(id,name,modifiedTime,createdTime,owners,webViewLink),nextPageToken,resultSizeEstimate",
    }
    if page_token:
        params["pageToken"] = page_token

    result = _api_call(lambda: drive.files().list(**params).execute())
    files = result.get("files", [])
    sheets = [standardize_sheet_response(f) for f in files]

    _sheet_context.add_search_results(sheets)

    return paginated_response(
        items=sheets,
        next_page_token=result.get("nextPageToken", ""),
        has_more=bool(result.get("nextPageToken")),
        total_count=result.get("resultSizeEstimate", len(sheets)),
    )


# ═════════════════════════════════════════════════════════════════════════════
# DATA READ (with client-side pagination)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def read_sheet_data(
    sheet_id: str,
    tab: str = "",
    range_name: str = "",
    limit: int = 100,
    page_token: str = "",
    include_metadata: bool = False,
) -> dict:
    """
    Read spreadsheet data with client-side pagination.

    Args:
        sheet_id: Spreadsheet ID
        tab: Tab name (defaults to first tab if empty)
        range_name: A1 notation or natural language range (defaults to all data)
        limit: Max rows to return per call (up to MAX_ROWS_PER_READ)
        page_token: Row offset for pagination ("0", "100", "200" ...)
        include_metadata: Include tab list in response

    Returns:
        dict with rows, headers, range, has_more, next_page_token, column_types
    """
    service = authenticate_sheets()

    # Resolve tab name
    actual_tab = _resolve_first_tab(sheet_id) if not tab else tab
    # Resolve range
    actual_range = _build_range(sheet_id, actual_tab, range_name or "")

    cache_key = f"data:{sheet_id}:{actual_range}"
    all_rows = _sheets_cache.get(cache_key)

    if all_rows is None:
        result = _api_call(lambda: service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=actual_range,
        ).execute())
        all_rows = result.get("values", [])
        _sheets_cache.set(cache_key, all_rows, ttl=180)

    # Detect headers from first row
    headers = detect_headers(all_rows)
    data_rows = all_rows[1:] if headers else all_rows

    # Client-side pagination
    limit = min(limit, MAX_ROWS_PER_READ)
    offset = int(page_token) if page_token and page_token.isdigit() else 0
    page_rows = data_rows[offset: offset + limit]
    has_more = (offset + limit) < len(data_rows)
    next_token = str(offset + limit) if has_more else ""

    # Infer column types
    col_types = _infer_column_types(headers, data_rows[:50])

    # Update context
    _sheet_context.add_viewed(sheet_id, tab=actual_tab)
    _sheet_context.set_last_range(actual_range, headers=headers)

    response = {
        "sheet_id": sheet_id,
        "tab": actual_tab,
        "range": actual_range,
        "headers": headers,
        "rows": page_rows,
        "column_types": col_types,
        "row_count": len(data_rows),
        "returned_count": len(page_rows),
        "has_more": has_more,
        "next_page_token": next_token,
        "current_offset": offset,
    }

    if include_metadata:
        response["metadata"] = get_sheet_metadata(sheet_id)

    return response


def _resolve_first_tab(sheet_id: str) -> str:
    """Return the name of the first tab in a spreadsheet."""
    service = authenticate_sheets()
    meta = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties.title"
    ).execute())
    sheets = meta.get("sheets", [])
    return sheets[0]["properties"]["title"] if sheets else "Sheet1"


def _build_range(sheet_id: str, tab: str, range_name: str) -> str:
    """Build a fully qualified range string."""
    if not range_name:
        return tab  # Full tab read
    parsed = parse_natural_range(
        range_name,
        headers=_sheet_context.last_read_headers,
    )
    if "!" in parsed:
        return parsed
    return f"{tab}!{parsed}"


def _infer_column_types(headers: list[str], sample_rows: list) -> dict:
    """Infer data types for each column based on sample rows."""
    if not headers or not sample_rows:
        return {}
    types: dict[str, str] = {}
    for col_idx, header in enumerate(headers):
        col_vals = [row[col_idx] if col_idx < len(row) else "" for row in sample_rows]
        non_empty = [v for v in col_vals if v and str(v).strip()]
        if not non_empty:
            types[header] = "empty"
            continue
        numeric_count = sum(1 for v in non_empty
                            if re.match(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$",
                                        str(v).replace(",", "").replace("$", "").replace("%", "").strip()))
        if numeric_count / len(non_empty) >= 0.8:
            types[header] = "numeric"
        elif all(re.match(r"^\d{4}-\d{2}-\d{2}", str(v)) for v in non_empty[:5]):
            types[header] = "date"
        else:
            types[header] = "text"
    return types


# ═════════════════════════════════════════════════════════════════════════════
# WRITE OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def write_range(
    sheet_id: str,
    range_name: str,
    values: list,
    require_confirmation: bool = False,
) -> dict:
    """Overwrite a range with values. Raises SheetSafetyError for large writes."""
    new_rows = len(values)
    needs_confirm, warning = check_write_safety(new_rows, require_confirmation=require_confirmation)
    if needs_confirm:
        raise SheetSafetyError(f"Write safety check failed: {warning}")

    service = authenticate_sheets()
    result = _api_call(lambda: service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute())

    invalidate_cache(f"data:{sheet_id}")

    return {
        "success": True,
        "sheet_id": sheet_id,
        "updated_range": result.get("updatedRange", range_name),
        "updated_rows": result.get("updatedRows", 0),
        "updated_cols": result.get("updatedColumns", 0),
        "updated_cells": result.get("updatedCells", 0),
    }


@log_tool_call
def append_rows(sheet_id: str, range_name: str, values: list) -> dict:
    """Append rows after the last row with data in the given range."""
    if not values:
        raise SheetValidationError("values", values, "values cannot be empty")

    service = authenticate_sheets()
    result = _api_call(lambda: service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute())

    invalidate_cache(f"data:{sheet_id}")
    updates = result.get("updates", {})

    return {
        "success": True,
        "sheet_id": sheet_id,
        "appended_range": updates.get("updatedRange", ""),
        "appended_rows": updates.get("updatedRows", 0),
        "appended_cells": updates.get("updatedCells", 0),
    }


@log_tool_call
def update_cell(sheet_id: str, cell: str, value: Any) -> dict:
    """Write a single cell value (or formula)."""
    service = authenticate_sheets()
    result = _api_call(lambda: service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute())

    invalidate_cache(f"data:{sheet_id}")

    return {
        "success": True,
        "sheet_id": sheet_id,
        "cell": cell,
        "value": value,
        "updated_range": result.get("updatedRange", cell),
    }


@log_tool_call
def clear_range(
    sheet_id: str,
    range_name: str,
    require_confirmation: bool = True,
) -> dict:
    """Clear all values from a range."""
    if require_confirmation:
        raise SheetSafetyError(
            f"Clearing range '{range_name}' is destructive. "
            "Set require_confirmation=False to proceed."
        )

    service = authenticate_sheets()
    result = _api_call(lambda: service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=range_name,
    ).execute())

    invalidate_cache(f"data:{sheet_id}")

    return {
        "success": True,
        "sheet_id": sheet_id,
        "cleared_range": result.get("clearedRange", range_name),
    }


# ═════════════════════════════════════════════════════════════════════════════
# BATCH OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def update_multiple_ranges(
    sheet_id: str,
    updates: list,
    require_confirmation: bool = False,
) -> dict:
    """
    Update multiple ranges in a single API call.

    Args:
        sheet_id: Spreadsheet ID
        updates: List of {"range": str, "values": [[...]]} dicts
        require_confirmation: safety gate for large writes

    Returns:
        dict with succeeded/failed per-range
    """
    if not updates:
        raise SheetValidationError("updates", updates, "updates list cannot be empty")
    if len(updates) > BATCH_RANGE_LIMIT:
        raise SheetValidationError("updates", len(updates),
                                   f"Max {BATCH_RANGE_LIMIT} ranges per batch")

    total_rows = sum(len(u.get("values", [])) for u in updates)
    needs_confirm, warning = check_write_safety(total_rows, require_confirmation=require_confirmation)
    if needs_confirm:
        raise SheetSafetyError(f"Batch write safety check: {warning}")

    data = [
        {
            "range": u["range"],
            "values": u["values"],
            "majorDimension": "ROWS",
        }
        for u in updates
        if u.get("range") and u.get("values")
    ]

    service = authenticate_sheets()
    result = _api_call(lambda: service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute())

    invalidate_cache(f"data:{sheet_id}")

    responses = result.get("responses", [])
    return {
        "success": True,
        "sheet_id": sheet_id,
        "ranges_updated": len(responses),
        "total_cells_updated": sum(r.get("updatedCells", 0) for r in responses),
        "total_rows_updated": sum(r.get("updatedRows", 0) for r in responses),
    }


@log_tool_call
def append_rows_bulk(sheet_id: str, tab: str, rows_list: list) -> dict:
    """
    Bulk-append many rows using a single API call.

    Args:
        sheet_id: Spreadsheet ID
        tab: Target tab name
        rows_list: List of rows (each row is a list of cell values)
    """
    if not rows_list:
        raise SheetValidationError("rows_list", rows_list, "rows_list cannot be empty")
    if len(rows_list) > BATCH_ROW_LIMIT:
        raise SheetValidationError("rows_list", len(rows_list),
                                   f"Max {BATCH_ROW_LIMIT} rows per bulk append")

    return append_rows(sheet_id, range_name=tab, values=rows_list)


@log_tool_call
def delete_rows_bulk(
    sheet_id: str,
    tab: str = "",
    row_indices: list = None,
    require_confirmation: bool = True,
) -> dict:
    """
    Delete specific rows by 1-based row index.

    Processes rows in reverse order to prevent index shifting.

    Args:
        sheet_id: Spreadsheet ID
        tab: Tab name (required to get numeric sheetId)
        row_indices: List of 1-based row numbers to delete
        require_confirmation: safety gate for deletions > 10 rows
    """
    if not row_indices:
        raise SheetValidationError("row_indices", row_indices, "row_indices cannot be empty")

    needs_confirm, warning = check_delete_safety(len(row_indices), require_confirmation)
    if needs_confirm:
        raise SheetSafetyError(warning)

    # Get numeric sheet (tab) ID
    service = authenticate_sheets()
    tab_name = tab or _resolve_first_tab(sheet_id)
    meta = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties"
    ).execute())

    numeric_tab_id = None
    for s in meta.get("sheets", []):
        if s["properties"]["title"].lower() == tab_name.lower():
            numeric_tab_id = s["properties"]["sheetId"]
            break

    if numeric_tab_id is None:
        raise SheetNotFoundError(f"Tab '{tab_name}' not found")

    # Sort descending to delete from bottom up (avoids index shift)
    sorted_indices = sorted(set(row_indices), reverse=True)

    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": numeric_tab_id,
                    "dimension": "ROWS",
                    "startIndex": idx - 1,   # 0-based
                    "endIndex": idx,
                }
            }
        }
        for idx in sorted_indices
    ]

    _api_call(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": requests},
    ).execute())

    invalidate_cache(f"data:{sheet_id}")

    return {
        "success": True,
        "sheet_id": sheet_id,
        "tab": tab_name,
        "rows_deleted": len(sorted_indices),
        "deleted_indices": sorted_indices,
    }


# ═════════════════════════════════════════════════════════════════════════════
# FORMULA SUPPORT
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def insert_formula(sheet_id: str, cell: str, formula: str) -> dict:
    """
    Insert a formula into a specific cell.

    Args:
        sheet_id: Spreadsheet ID
        cell: Target cell in A1 notation (e.g. "Sheet1!B2" or "B2")
        formula: Formula string starting with "=" (e.g. "=SUM(A:A)")
    """
    is_valid, error_msg = check_formula_safety(formula)
    if not is_valid:
        raise SheetValidationError("formula", formula, error_msg)

    return update_cell(sheet_id, cell=cell, value=formula)


@log_tool_call
def detect_formula_columns(sheet_id: str, tab: str = "") -> dict:
    """
    Detect which columns contain formulas by reading cell metadata.

    Returns:
        dict with formula_cells list and column summary
    """
    service = authenticate_sheets()
    tab_name = tab or _resolve_first_tab(sheet_id)

    result = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        ranges=[tab_name],
        includeGridData=True,
        fields="sheets.data.rowData.values(formattedValue,formula)",
    ).execute())

    formula_cells = []
    sheets_data = result.get("sheets", [{}])[0].get("data", [{}])[0]
    for row_idx, row_data in enumerate(sheets_data.get("rowData", [])):
        for col_idx, cell in enumerate(row_data.get("values", [])):
            formula = cell.get("formula", "")
            if formula:
                formula_cells.append({
                    "cell": f"{col_index_to_letter(col_idx)}{row_idx + 1}",
                    "formula": formula,
                })

    formula_cols = list({c["cell"][0] for c in formula_cells})

    return {
        "sheet_id": sheet_id,
        "tab": tab_name,
        "formula_cells": formula_cells,
        "formula_columns": formula_cols,
        "formula_count": len(formula_cells),
    }


@log_tool_call
def compute_column_summary(sheet_id: str, tab: str = "", column: str = "") -> dict:
    """
    Compute a SUM, AVERAGE, MIN, MAX summary for a column by inserting
    formulas into the first empty row below the data.

    Args:
        sheet_id: Spreadsheet ID
        tab: Tab name
        column: Column letter (e.g. "B") or column name if headers exist

    Returns:
        dict with inserted formula range and computed range
    """
    raw = read_sheet_data(sheet_id, tab=tab, limit=5)
    headers = raw.get("headers", [])
    tab_name = raw.get("tab", tab)
    row_count = raw.get("row_count", 0)

    # Resolve column letter
    col_letter = column.upper() if re.match(r"^[A-Z]+$", column.upper()) else ""
    if not col_letter and headers:
        idx = get_column_index(headers, column)
        if idx is not None:
            col_letter = col_index_to_letter(idx)

    if not col_letter:
        raise SheetValidationError("column", column, "Could not resolve column letter")

    # Insert formulas one row below data
    summary_row = row_count + 2  # +1 for header, +1 for spacing
    formulas = [
        [f"=SUM({col_letter}2:{col_letter}{row_count + 1})"],
        [f"=AVERAGE({col_letter}2:{col_letter}{row_count + 1})"],
        [f"=MIN({col_letter}2:{col_letter}{row_count + 1})"],
        [f"=MAX({col_letter}2:{col_letter}{row_count + 1})"],
    ]
    labels = ["SUM", "AVERAGE", "MIN", "MAX"]

    results = {}
    for i, (label, formula_row) in enumerate(zip(labels, formulas)):
        target = f"{tab_name}!{col_letter}{summary_row + i}"
        insert_formula(sheet_id, cell=target, formula=formula_row[0])
        results[label] = target

    return {
        "success": True,
        "sheet_id": sheet_id,
        "tab": tab_name,
        "column": col_letter,
        "summary_cells": results,
    }


# ═════════════════════════════════════════════════════════════════════════════
# DATA ANALYSIS
# ═════════════════════════════════════════════════════════════════════════════

def analyze_sheet_summary(
    sheet_id: str,
    rows: list = None,
    headers: list = None,
    raw_meta: dict = None,
) -> dict:
    """Generate a high-level summary of a sheet's data."""
    if rows is None:
        raw_meta = read_sheet_data(sheet_id, limit=MAX_ROWS_PER_READ)
        rows = raw_meta.get("rows", [])
        headers = raw_meta.get("headers", [])

    total_cells = sum(len(row) for row in rows)
    empty_cells = sum(
        1 for row in rows for cell in row
        if not cell and cell != 0
    )
    fill_rate = round((total_cells - empty_cells) / total_cells, 3) if total_cells else 0

    col_types = _infer_column_types(headers, rows[:50])

    _, last_row, _, last_col = detect_data_range(rows)

    return {
        "analysis_type": "summary",
        "sheet_id": sheet_id,
        "tab": raw_meta.get("tab", "") if raw_meta else "",
        "headers": headers,
        "row_count": len(rows),
        "column_count": len(headers) if headers else (len(rows[0]) if rows else 0),
        "total_cells": total_cells,
        "empty_cells": empty_cells,
        "fill_rate": fill_rate,
        "column_types": col_types,
        "data_end_row": last_row + 1,
    }


def analyze_column_stats(
    rows: list,
    headers: list,
    column: str = "",
) -> dict:
    """Compute statistics for a specific column."""
    if not column:
        raise SheetValidationError("column", column, "column is required for column_stats")

    col_idx = None
    col_label = column

    if re.match(r"^[A-Za-z]+$", column):
        col_idx = col_letter_to_index(column.upper())
        if headers and col_idx < len(headers):
            col_label = headers[col_idx]
    elif headers:
        col_idx = get_column_index(headers, column)
        col_label = column

    if col_idx is None:
        raise SheetRangeError(f"Column '{column}' not found")

    values = [row[col_idx] if col_idx < len(row) else "" for row in rows]
    stats = compute_column_stats(values)

    return {
        "analysis_type": "column_stats",
        "column": col_label,
        "column_index": col_idx,
        **stats,
    }


def detect_missing_values(rows: list, headers: list) -> dict:
    """Identify rows and columns with missing (empty) values."""
    col_count = len(headers) if headers else (len(rows[0]) if rows else 0)

    empty_by_col: dict[str, int] = {}
    rows_with_missing: list[int] = []

    for row_idx, row in enumerate(rows):
        row_missing = False
        for col_idx in range(col_count):
            cell = row[col_idx] if col_idx < len(row) else ""
            if not cell and cell != 0:
                col_name = headers[col_idx] if headers and col_idx < len(headers) else col_index_to_letter(col_idx)
                empty_by_col[col_name] = empty_by_col.get(col_name, 0) + 1
                row_missing = True
        if row_missing:
            rows_with_missing.append(row_idx + 2)  # 1-based + header offset

    total = len(rows) * col_count
    total_missing = sum(empty_by_col.values())

    return {
        "analysis_type": "missing_values",
        "total_rows": len(rows),
        "rows_with_missing": rows_with_missing[:50],
        "rows_affected": len(rows_with_missing),
        "missing_by_column": empty_by_col,
        "total_missing_cells": total_missing,
        "missing_rate": round(total_missing / total, 3) if total else 0,
    }


def detect_outliers(rows: list, headers: list, column: str = "") -> dict:
    """Detect numeric outliers in a column using IQR method."""
    if not column:
        raise SheetValidationError("column", column, "column is required for outliers analysis")

    col_idx = None
    if re.match(r"^[A-Za-z]+$", column):
        col_idx = col_letter_to_index(column.upper())
    elif headers:
        col_idx = get_column_index(headers, column)

    if col_idx is None:
        raise SheetRangeError(f"Column '{column}' not found")

    values = [row[col_idx] if col_idx < len(row) else "" for row in rows]
    outliers = detect_outliers_in_column(values)

    col_label = (headers[col_idx] if headers and col_idx < len(headers) else column)

    return {
        "analysis_type": "outliers",
        "column": col_label,
        "column_index": col_idx,
        "outlier_count": len(outliers),
        "outliers": outliers,
        "rows_analyzed": len(values),
    }


# ═════════════════════════════════════════════════════════════════════════════
# TAB MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def add_tab(sheet_id: str, tab_name: str) -> dict:
    """Add a new tab to the spreadsheet."""
    service = authenticate_sheets()
    result = _api_call(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute())

    replies = result.get("replies", [{}])
    new_props = replies[0].get("addSheet", {}).get("properties", {})

    invalidate_cache(f"meta:{sheet_id}")

    return {
        "success": True,
        "sheet_id": sheet_id,
        "tab_name": new_props.get("title", tab_name),
        "tab_id": new_props.get("sheetId", ""),
    }


@log_tool_call
def rename_tab(sheet_id: str, old_name: str, new_name: str) -> dict:
    """Rename an existing tab."""
    service = authenticate_sheets()

    meta = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties",
    ).execute())

    tab_id = None
    for s in meta.get("sheets", []):
        if s["properties"]["title"].lower() == old_name.lower():
            tab_id = s["properties"]["sheetId"]
            break

    if tab_id is None:
        raise SheetNotFoundError(f"Tab '{old_name}' not found")

    _api_call(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": tab_id, "title": new_name},
            "fields": "title",
        }}]},
    ).execute())

    invalidate_cache(f"meta:{sheet_id}")

    return {"success": True, "sheet_id": sheet_id, "old_name": old_name, "new_name": new_name}


@log_tool_call
def delete_tab(sheet_id: str, tab_name: str, require_confirmation: bool = True) -> dict:
    """Delete a tab from the spreadsheet."""
    if require_confirmation:
        raise SheetSafetyError(
            f"Deleting tab '{tab_name}' is irreversible. "
            "Set require_confirmation=False to proceed."
        )

    service = authenticate_sheets()

    meta = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="sheets.properties",
    ).execute())

    tab_id = None
    for s in meta.get("sheets", []):
        if s["properties"]["title"].lower() == tab_name.lower():
            tab_id = s["properties"]["sheetId"]
            break

    if tab_id is None:
        raise SheetNotFoundError(f"Tab '{tab_name}' not found")

    _api_call(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"deleteSheet": {"sheetId": tab_id}}]},
    ).execute())

    invalidate_cache(f"meta:{sheet_id}")
    invalidate_cache(f"data:{sheet_id}")

    return {"success": True, "sheet_id": sheet_id, "deleted_tab": tab_name}


# ═════════════════════════════════════════════════════════════════════════════
# SPREADSHEET LIFECYCLE
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def create_spreadsheet(title: str, tab_names: list = None) -> dict:
    """
    Create a new Google Spreadsheet, optionally with named tabs.

    Args:
        title: Spreadsheet title
        tab_names: List of tab names to create (default is ["Sheet1"])
    """
    is_valid, error = validate_sheet_title(title)
    if not is_valid:
        raise SheetValidationError("title", title, error)

    service = authenticate_sheets()

    body: dict = {"properties": {"title": title}}
    if tab_names:
        body["sheets"] = [
            {"properties": {"title": name, "index": i}}
            for i, name in enumerate(tab_names)
        ]

    result = _api_call(lambda: service.spreadsheets().create(
        body=body,
        fields="spreadsheetId,properties.title",
    ).execute())

    sheet_id = result.get("spreadsheetId", "")
    return {
        "success": True,
        "id": sheet_id,
        "title": result.get("properties", {}).get("title", title),
        "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        "tabs": tab_names or ["Sheet1"],
    }


@log_tool_call
def delete_spreadsheet(sheet_id: str, require_confirmation: bool = True) -> dict:
    """Move a spreadsheet to Drive trash."""
    if require_confirmation:
        raise SheetSafetyError(
            f"Deleting spreadsheet {sheet_id} moves it to trash. "
            "Set require_confirmation=False to proceed."
        )

    drive = authenticate_drive()
    _api_call(lambda: drive.files().update(
        fileId=sheet_id,
        body={"trashed": True},
    ).execute())

    invalidate_cache(f"meta:{sheet_id}")
    invalidate_cache(f"data:{sheet_id}")

    return {"success": True, "sheet_id": sheet_id, "status": "moved_to_trash"}


# ═════════════════════════════════════════════════════════════════════════════
# LLM-POWERED ANALYSIS HELPER
# ═════════════════════════════════════════════════════════════════════════════

def _llm_analyze_sheet(rows: list, headers: list, analysis_type: str) -> Optional[dict]:
    """
    Run LLM-powered sheet analysis. Returns parsed dict or None on failure.
    analysis_type: "summary" | "insights" | "anomalies"
    """
    prompts = {
        "summary": (
            "Analyze this spreadsheet data and return a JSON summary:\n"
            "{\"summary\": \"...\", \"key_observations\": [\"...\"], \"data_quality\": \"good|fair|poor\"}\n\n"
        ),
        "insights": (
            "Identify key insights and trends in this spreadsheet data. "
            "Return JSON: {\"insights\": [\"...\"], \"trends\": [\"...\"]}\n\n"
        ),
        "anomalies": (
            "Identify any anomalies, outliers, or suspicious entries in this data. "
            "Return JSON: {\"anomalies\": [{\"description\": \"...\", \"rows\": []}]}\n\n"
        ),
    }

    prompt = prompts.get(analysis_type)
    if not prompt:
        return None

    # Build compact text representation
    preview_rows = [headers] + rows[:30] if headers else rows[:30]
    data_text = "\n".join(",".join(str(c) for c in row) for row in preview_rows)

    messages = [
        {"role": "system", "content": "You are a data analyst. Always respond with valid JSON only."},
        {"role": "user", "content": prompt + f"Data (CSV preview):\n{data_text}"},
    ]

    try:
        raw = call_model(messages)
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass

    return None


@log_tool_call
def get_sheet_insights(
    sheet_id: str,
    sheet_reference: str = "",
    tab: str = "",
    analysis_type: str = "insights",
) -> dict:
    """
    LLM-powered sheet analysis for high-level insights and anomaly detection.

    Args:
        sheet_id / sheet_reference: target spreadsheet
        tab: tab name
        analysis_type: "summary" | "insights" | "anomalies"
    """
    if not sheet_id and sheet_reference:
        sheet_id = resolve_sheet_id(sheet_reference)

    raw = read_sheet_data(sheet_id, tab=tab, limit=50)
    rows = raw.get("rows", [])
    headers = raw.get("headers", [])

    llm_result = _llm_analyze_sheet(rows, headers, analysis_type)

    if llm_result:
        return {
            "sheet_id": sheet_id,
            "tab": raw.get("tab", tab),
            "analysis_type": analysis_type,
            "source": "llm",
            **llm_result,
        }

    # Fallback to rule-based summary
    return analyze_sheet_summary(sheet_id, rows=rows, headers=headers, raw_meta=raw)


# ═════════════════════════════════════════════════════════════════════════════
# CONTEXT & STATISTICS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_sheets_context_summary() -> dict:
    """Return current context: active sheet, recent results, last range and headers."""
    return _sheet_context.get_context_summary()


@log_tool_call
def get_sheets_stats() -> dict:
    """Get Sheets MCP usage statistics including per-tool breakdown and recent errors."""
    return {
        "logger_stats": _sheets_logger.get_stats(),
        "tool_stats": _sheets_logger.get_tool_stats(),
        "recent_errors": _sheets_logger.get_recent_errors(n=5),
        "cache_stats": _sheets_cache.stats(),
        "context": {
            "current_sheet": _sheet_context.current_sheet_id,
            "current_tab": _sheet_context.current_tab,
            "last_range": _sheet_context.last_range,
            "tracked_sheets": len(_sheet_context.last_viewed_ids),
        },
    }


@log_tool_call
def clear_sheets_cache(prefix: str = "") -> dict:
    """Clear cached sheet data. Pass prefix to target specific sheets."""
    return invalidate_cache(prefix)


# ═════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY WRAPPERS
# ═════════════════════════════════════════════════════════════════════════════

def list_sheets(limit: int = 10) -> list[dict]:
    result = list_spreadsheets(limit=limit)
    return result.get("items", [])


def search_sheets(query: str, limit: int = 10) -> list[dict]:
    result = search_spreadsheets(query=query, limit=limit)
    return result.get("items", [])


def get_sheet(sheet_id: str) -> dict:
    return get_sheet_metadata(sheet_id)


def read_sheet(sheet_id: str, range_name: str = "Sheet1") -> dict:
    tab, rng = (range_name.split("!", 1) + [""])[:2]
    raw = read_sheet_data(sheet_id, tab=tab or range_name, range_name=rng)
    all_rows = ([raw["headers"]] + raw["rows"]) if raw.get("headers") else raw["rows"]
    return {
        "range": raw.get("range", range_name),
        "rows": len(all_rows),
        "cols": max((len(r) for r in all_rows), default=0),
        "values": all_rows,
    }


def create_sheet(title: str) -> dict:
    return create_spreadsheet(title)


def write_to_sheet(sheet_id: str, range_name: str, values: list) -> dict:
    return write_range(sheet_id, range_name=range_name, values=values, require_confirmation=False)


def append_to_sheet(sheet_id: str, range_name: str, values: list) -> dict:
    return append_rows(sheet_id, range_name=range_name, values=values)


def clear_sheet_range(sheet_id: str, range_name: str) -> dict:
    return clear_range(sheet_id, range_name=range_name, require_confirmation=False)


def add_sheet_tab(sheet_id: str, tab_name: str) -> dict:
    return add_tab(sheet_id, tab_name=tab_name)


def rename_sheet_tab(sheet_id: str, old_name: str, new_name: str) -> dict:
    return rename_tab(sheet_id, old_name=old_name, new_name=new_name)


def delete_sheet(sheet_id: str) -> dict:
    return delete_spreadsheet(sheet_id, require_confirmation=False)
