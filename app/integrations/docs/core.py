"""
Google Docs & Drive integration for G-Assistant.

Auth note: requires ALL_SCOPES (documents + drive).
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
    """Load and refresh credentials from token file."""
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


def _check_docs_scope(creds: Credentials) -> bool:
    """Return True if the token includes the Docs scope."""
    scopes = getattr(creds, "scopes", None) or []
    return any("documents" in s for s in scopes)


def authenticate_docs():
    """Return a thread-local Google Docs API service."""
    if getattr(_thread_local, "docs_service", None) is not None:
        return _thread_local.docs_service
    creds = _load_creds()
    if not creds:
        raise PermissionError(
            "Google Docs not authenticated. Delete credentials/token.json, "
            "run `python auth.py`, then restart the app."
        )
    _thread_local.docs_service = build("docs", "v1", credentials=creds)
    return _thread_local.docs_service


def authenticate_drive():
    """Return a thread-local Google Drive API service."""
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


def _reset_services():
    _thread_local.docs_service  = None
    _thread_local.drive_service = None


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
            raise RuntimeError(f"API error ({status}): {exc}") from exc
        except (OSError, ConnectionError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"API call failed after {retries} attempts: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction helpers
# ─────────────────────────────────────────────────────────────────────────────

def _extract_text(doc: dict, max_chars: int = 4000) -> str:
    """Pull plain text from a Google Docs document object."""
    parts: list[str] = []
    for element in doc.get("body", {}).get("content", []):
        para = element.get("paragraph")
        if para:
            for pe in para.get("elements", []):
                tr = pe.get("textRun")
                if tr:
                    parts.append(tr.get("content", ""))
    text = "".join(parts)
    if len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated]"
    return text


def _doc_end_index(doc: dict) -> int:
    """Return the last writable index in a document body."""
    content = doc.get("body", {}).get("content", [])
    if content:
        return max(1, content[-1].get("endIndex", 1) - 1)
    return 1


def _normalize_doc(doc: dict) -> dict:
    return {
        "id":    doc.get("documentId", ""),
        "title": doc.get("title", "(Untitled)"),
        "url":   f"https://docs.google.com/document/d/{doc.get('documentId', '')}/edit",
        "text":  _extract_text(doc),
    }


# ─────────────────────────────────────────────────────────────────────────────
# List / Search
# ─────────────────────────────────────────────────────────────────────────────

def list_docs(limit: int = 10) -> list[dict]:
    """List recent Google Docs (most recently modified first)."""
    drive   = authenticate_drive()
    query   = "mimeType='application/vnd.google-apps.document' and trashed=false"
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
            "modified": f.get("modifiedTime", ""),
            "url":      f.get("webViewLink", ""),
        }
        for f in results.get("files", [])
    ]


def search_docs(query: str, limit: int = 10) -> list[dict]:
    """Search Google Docs by full-text content or title."""
    drive = authenticate_drive()
    safe  = query.replace("'", " ")   # avoid query injection
    drive_query = (
        f"mimeType='application/vnd.google-apps.document' and trashed=false "
        f"and fullText contains '{safe}'"
    )
    results = _api_call(lambda: drive.files().list(
        q=drive_query,
        pageSize=min(limit, 20),
        orderBy="modifiedTime desc",
        fields="files(id,name,modifiedTime,webViewLink)",
    ).execute())
    return [
        {
            "id":       f["id"],
            "title":    f["name"],
            "modified": f.get("modifiedTime", ""),
            "url":      f.get("webViewLink", ""),
        }
        for f in results.get("files", [])
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Read
# ─────────────────────────────────────────────────────────────────────────────

def get_doc(doc_id: str) -> dict:
    """Fetch and return a document's title + full text content."""
    service = authenticate_docs()
    doc     = _api_call(lambda: service.documents().get(documentId=doc_id).execute())
    return _normalize_doc(doc)


def get_doc_content(doc_id: str) -> str:
    """Return only the plain-text body of a document."""
    return get_doc(doc_id).get("text", "")


# ─────────────────────────────────────────────────────────────────────────────
# Create / Write
# ─────────────────────────────────────────────────────────────────────────────

def create_doc(title: str, content: str = "") -> dict:
    """Create a new Google Doc, optionally pre-filling it with content."""
    service = authenticate_docs()
    doc     = _api_call(lambda: service.documents().create(
        body={"title": title}
    ).execute())
    doc_id  = doc["documentId"]

    if content:
        _api_call(lambda: service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
        ).execute())

    return {
        "id":      doc_id,
        "title":   title,
        "url":     f"https://docs.google.com/document/d/{doc_id}/edit",
        "message": f"Document '{title}' created successfully.",
    }


def append_to_doc(doc_id: str, text: str) -> dict:
    """Append text (with a leading newline) to the end of a document."""
    service  = authenticate_docs()
    doc      = _api_call(lambda: service.documents().get(documentId=doc_id).execute())
    end_idx  = _doc_end_index(doc)
    _api_call(lambda: service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": end_idx}, "text": "\n" + text}}]},
    ).execute())
    return {"success": True, "message": f"Text appended to document '{doc.get('title', doc_id)}'."}


def replace_text_in_doc(doc_id: str, find: str, replace: str) -> dict:
    """
    Find and replace all occurrences of a string in a document.
    
    IMPORTANT: The 'find' text must match EXACTLY including whitespace and newlines.
    If no matches are found, the function will return the actual document content
    to help you see what text is actually in the document.
    """
    service = authenticate_docs()
    
    # Get current document content first for feedback
    doc = _api_call(lambda: service.documents().get(documentId=doc_id).execute())
    current_text = _extract_text(doc, max_chars=10000)
    
    # Perform the replacement
    result  = _api_call(lambda: service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": False},
                "replaceText":  replace,
            }
        }]},
    ).execute())
    
    n = (result.get("replies") or [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
    
    # Build helpful response
    response = {
        "success": n > 0,
        "occurrences_replaced": n,
        "find_text": find[:100] + "..." if len(find) > 100 else find,
    }
    
    if n == 0:
        # Provide helpful feedback when no matches
        response["warning"] = "No matches found. The 'find' text must match EXACTLY."
        response["document_content_preview"] = current_text[:500] + "..." if len(current_text) > 500 else current_text
        response["suggestion"] = "Tips: (1) Check for extra spaces/newlines (2) Use doc_modify(action='clear') + doc_modify(action='append') for full replacement (3) Use replace_section() for heading-based replacement"
    else:
        response["message"] = f"Successfully replaced {n} occurrence(s) of text."
    
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Rename / Delete
# ─────────────────────────────────────────────────────────────────────────────

def update_doc_title(doc_id: str, new_title: str) -> dict:
    """Rename a Google Doc."""
    drive  = authenticate_drive()
    result = _api_call(lambda: drive.files().update(
        fileId=doc_id,
        body={"name": new_title},
        fields="id,name",
    ).execute())
    return {"success": True, "id": result["id"], "title": result["name"]}


def delete_doc(doc_id: str) -> dict:
    """Move a Google Doc to the Drive trash."""
    drive = authenticate_drive()
    _api_call(lambda: drive.files().update(
        fileId=doc_id,
        body={"trashed": True},
    ).execute())
    return {"success": True, "message": f"Document {doc_id} moved to trash."}
