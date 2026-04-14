"""
Google Sheets & Drive integration for G-Assistant.

Auth note: requires ALL_SCOPES (spreadsheets + drive).
If authentication fails, delete credentials/token.json and run `python auth.py`.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from app.core.config import TOKEN_FILE, ALL_SCOPES

_thread_local = threading.local()


# ─────────────────────────────────────────────────────────────────────────────
# Auth helpers
# ─────────────────────────────────────────────────────────────────────────────

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
    _thread_local.drive_service  = None


def authenticate_sheets():
    """Return a thread-local Google Sheets API service."""
    if getattr(_thread_local, "sheets_service", None) is not None:
        return _thread_local.sheets_service
    creds = _load_creds()
    if not creds:
        raise PermissionError(
            "Google Sheets not authenticated. Delete credentials/token.json, "
            "run `python auth.py`, then restart the app."
        )
    _thread_local.sheets_service = build("sheets", "v4", credentials=creds)
    return _thread_local.sheets_service


def authenticate_drive():
    """Return a thread-local Google Drive API service (for listing/searching)."""
    if getattr(_thread_local, "drive_service", None) is not None:
        return _thread_local.drive_service
    creds = _load_creds()
    if not creds:
        raise PermissionError(
            "Google Drive not authenticated. Delete credentials/token.json, "
            "run `python auth.py`, then restart the app."
        )
    _thread_local.drive_service = build("drive", "v3", credentials=creds)
    return _thread_local.drive_service


# ─────────────────────────────────────────────────────────────────────────────
# Retry wrapper
# ─────────────────────────────────────────────────────────────────────────────

def _api_call(fn, retries: int = 3, backoff: float = 1.5):
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
            if status in (429, 500, 502, 503, 504):
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Sheets API error ({status}): {exc}") from exc
        except (OSError, ConnectionError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"API call failed after {retries} attempts: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
# List / Search  (via Drive API)
# ─────────────────────────────────────────────────────────────────────────────

def list_sheets(limit: int = 10) -> list[dict]:
    """List recent Google Sheets spreadsheets (most recently modified first)."""
    drive   = authenticate_drive()
    query   = "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false"
    results = _api_call(lambda: drive.files().list(
        q=query,
        pageSize=min(limit, 20),
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime,webViewLink)",
    ).execute())
    return [
        {
            "id":       f["id"],
            "title":    f["name"],
            "modified": f.get("modifiedTime", "")[:10],
            "url":      f.get("webViewLink", ""),
        }
        for f in results.get("files", [])
    ]


def search_sheets(query: str, limit: int = 10) -> list[dict]:
    """Search Google Sheets by title or full-text content."""
    drive      = authenticate_drive()
    safe_query = query.replace("'", " ")
    drive_q    = (
        f"mimeType='application/vnd.google-apps.spreadsheet' and trashed=false "
        f"and fullText contains '{safe_query}'"
    )
    results = _api_call(lambda: drive.files().list(
        q=drive_q,
        pageSize=min(limit, 20),
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime,webViewLink)",
    ).execute())
    return [
        {
            "id":       f["id"],
            "title":    f["name"],
            "modified": f.get("modifiedTime", "")[:10],
            "url":      f.get("webViewLink", ""),
        }
        for f in results.get("files", [])
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def get_sheet(sheet_id: str) -> dict:
    """Get spreadsheet metadata: title, tab names, row/column counts."""
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().get(
        spreadsheetId=sheet_id,
        fields="spreadsheetId,properties,sheets.properties",
    ).execute())
    tabs = [
        {
            "name": s["properties"]["title"],
            "rows": s["properties"].get("gridProperties", {}).get("rowCount", 0),
            "cols": s["properties"].get("gridProperties", {}).get("columnCount", 0),
        }
        for s in result.get("sheets", [])
    ]
    return {
        "id":    result.get("spreadsheetId", sheet_id),
        "title": result.get("properties", {}).get("title", "(Untitled)"),
        "url":   f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        "tabs":  tabs,
    }


def read_sheet(sheet_id: str, range_name: str = "Sheet1") -> dict:
    """Read values from a spreadsheet range (e.g. 'Sheet1' or 'Sheet1!A1:D10')."""
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=range_name,
    ).execute())
    values = result.get("values", [])
    return {
        "range":  result.get("range", range_name),
        "rows":   len(values),
        "cols":   max((len(r) for r in values), default=0),
        "values": values,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Create / Write
# ─────────────────────────────────────────────────────────────────────────────

def create_sheet(title: str) -> dict:
    """Create a new Google Spreadsheet."""
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().create(
        body={"properties": {"title": title}},
        fields="spreadsheetId,properties.title",
    ).execute())
    sheet_id = result.get("spreadsheetId", "")
    return {
        "id":      sheet_id,
        "title":   result.get("properties", {}).get("title", title),
        "url":     f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
        "message": f"Spreadsheet '{title}' created successfully.",
    }


def write_to_sheet(sheet_id: str, range_name: str, values: list[list]) -> dict:
    """Write (overwrite) values to a spreadsheet range.
    values must be a 2-D list, e.g. [["Name", "Age"], ["Alice", 30]].
    """
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute())
    return {
        "updated_range": result.get("updatedRange", ""),
        "updated_rows":  result.get("updatedRows", 0),
        "updated_cols":  result.get("updatedColumns", 0),
        "updated_cells": result.get("updatedCells", 0),
    }


def append_to_sheet(sheet_id: str, range_name: str, values: list[list]) -> dict:
    """Append rows after the last row with data in the given range.
    values must be a 2-D list, e.g. [["Alice", 30], ["Bob", 25]].
    """
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute())
    updates = result.get("updates", {})
    return {
        "appended_range": updates.get("updatedRange", ""),
        "appended_rows":  updates.get("updatedRows", 0),
        "appended_cells": updates.get("updatedCells", 0),
    }


def clear_sheet_range(sheet_id: str, range_name: str) -> dict:
    """Clear all values in a spreadsheet range."""
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().values().clear(
        spreadsheetId=sheet_id,
        range=range_name,
    ).execute())
    return {
        "success":       True,
        "cleared_range": result.get("clearedRange", range_name),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tab / Sheet management
# ─────────────────────────────────────────────────────────────────────────────

def add_sheet_tab(sheet_id: str, tab_name: str) -> dict:
    """Add a new tab (sheet) to an existing spreadsheet."""
    service = authenticate_sheets()
    result  = _api_call(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute())
    replies   = result.get("replies", [{}])
    new_props = replies[0].get("addSheet", {}).get("properties", {})
    return {
        "success":  True,
        "tab_name": new_props.get("title", tab_name),
        "tab_id":   new_props.get("sheetId", ""),
    }


def rename_sheet_tab(sheet_id: str, old_name: str, new_name: str) -> dict:
    """Rename an existing tab inside a spreadsheet."""
    service = authenticate_sheets()
    # First, find the sheetId of the tab by name
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
        return {"success": False, "error": f"Tab '{old_name}' not found."}
    _api_call(lambda: service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": tab_id, "title": new_name},
            "fields": "title",
        }}]},
    ).execute())
    return {"success": True, "old_name": old_name, "new_name": new_name}


# ─────────────────────────────────────────────────────────────────────────────
# Delete
# ─────────────────────────────────────────────────────────────────────────────

def delete_sheet(sheet_id: str) -> dict:
    """Move a Google Spreadsheet to Drive trash."""
    drive = authenticate_drive()
    _api_call(lambda: drive.files().update(
        fileId=sheet_id,
        body={"trashed": True},
    ).execute())
    return {"success": True, "message": f"Spreadsheet {sheet_id} moved to trash."}
