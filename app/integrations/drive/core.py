"""
Google Drive integration for G-Assistant.

Provides general-purpose file and folder management across all of Google Drive,
independent of file type (unlike docs/core.py or sheets/core.py which are type-specific).

Auth note: requires ALL_SCOPES (drive scope is included).
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


def _reset_service() -> None:
    _thread_local.drive_service = None


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
                _reset_service()
                last_err = exc
                continue
            if status in (429, 500, 502, 503, 504):
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Drive API error ({status}): {exc}") from exc
        except (OSError, ConnectionError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Drive API call failed after {retries} attempts: {last_err}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_FOLDER_MIME = "application/vnd.google-apps.folder"

_MIME_LABELS = {
    "application/vnd.google-apps.document":     "Google Doc",
    "application/vnd.google-apps.spreadsheet":  "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.form":         "Google Form",
    "application/vnd.google-apps.folder":       "Folder",
    "application/pdf":                          "PDF",
    "image/png":                                "PNG Image",
    "image/jpeg":                               "JPEG Image",
    "text/plain":                               "Text File",
    "text/csv":                                 "CSV File",
    "application/zip":                          "ZIP Archive",
}


def _mime_label(mime: str) -> str:
    return _MIME_LABELS.get(mime, mime.split("/")[-1].upper())


def _normalize_file(f: dict) -> dict:
    return {
        "id":       f.get("id", ""),
        "name":     f.get("name", "(Untitled)"),
        "type":     _mime_label(f.get("mimeType", "")),
        "mime":     f.get("mimeType", ""),
        "modified": (f.get("modifiedTime") or "")[:10],
        "size":     f.get("size", ""),
        "parents":  f.get("parents", []),
        "url":      f.get("webViewLink") or f.get("webContentLink", ""),
        "starred":  f.get("starred", False),
        "trashed":  f.get("trashed", False),
    }


_FILE_FIELDS = "id,name,mimeType,modifiedTime,size,parents,webViewLink,webContentLink,starred,trashed"


# ─────────────────────────────────────────────────────────────────────────────
# List / Browse
# ─────────────────────────────────────────────────────────────────────────────

def list_files(limit: int = 10, folder_id: str = "") -> list[dict]:
    """List files in Drive (or inside a specific folder), most recently modified first."""
    drive = authenticate_drive()
    query = "trashed=false"
    if folder_id:
        query += f" and '{folder_id}' in parents"
    results = _api_call(lambda: drive.files().list(
        q=query,
        pageSize=min(limit, 50),
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    return [_normalize_file(f) for f in results.get("files", [])]


def list_folders(limit: int = 20) -> list[dict]:
    """List all folders in Drive, most recently modified first."""
    drive = authenticate_drive()
    results = _api_call(lambda: drive.files().list(
        q=f"mimeType='{_FOLDER_MIME}' and trashed=false",
        pageSize=min(limit, 50),
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    return [_normalize_file(f) for f in results.get("files", [])]


def get_folder_contents(folder_id: str, limit: int = 20) -> list[dict]:
    """List all files and subfolders inside a specific folder."""
    return list_files(limit=limit, folder_id=folder_id)


def get_starred_files(limit: int = 20) -> list[dict]:
    """List starred (favourited) files."""
    drive = authenticate_drive()
    results = _api_call(lambda: drive.files().list(
        q="starred=true and trashed=false",
        pageSize=min(limit, 50),
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    return [_normalize_file(f) for f in results.get("files", [])]


def get_recent_files(limit: int = 10) -> list[dict]:
    """List the most recently modified files in Drive."""
    return list_files(limit=limit)


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

def search_files(query: str, limit: int = 10) -> list[dict]:
    """Search files by name or full-text content."""
    drive     = authenticate_drive()
    safe      = query.replace("'", " ")
    drive_q   = f"(name contains '{safe}' or fullText contains '{safe}') and trashed=false"
    results = _api_call(lambda: drive.files().list(
        q=drive_q,
        pageSize=min(limit, 50),
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    return [_normalize_file(f) for f in results.get("files", [])]


def search_files_by_type(file_type: str, limit: int = 10) -> list[dict]:
    """
    Search files by type keyword.
    file_type examples: 'doc', 'sheet', 'pdf', 'image', 'folder', 'slides'
    """
    _type_map = {
        "doc":      "application/vnd.google-apps.document",
        "docs":     "application/vnd.google-apps.document",
        "sheet":    "application/vnd.google-apps.spreadsheet",
        "sheets":   "application/vnd.google-apps.spreadsheet",
        "slides":   "application/vnd.google-apps.presentation",
        "pdf":      "application/pdf",
        "image":    "image/",
        "folder":   _FOLDER_MIME,
    }
    drive = authenticate_drive()
    mime  = _type_map.get(file_type.lower(), "")
    if mime and not mime.endswith("/"):
        query = f"mimeType='{mime}' and trashed=false"
    elif mime:
        query = f"mimeType contains '{mime}' and trashed=false"
    else:
        return search_files(file_type, limit=limit)
    results = _api_call(lambda: drive.files().list(
        q=query,
        pageSize=min(limit, 50),
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    return [_normalize_file(f) for f in results.get("files", [])]


# ─────────────────────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────────────────────

def get_file_metadata(file_id: str) -> dict:
    """Get full metadata for a file or folder."""
    drive  = authenticate_drive()
    result = _api_call(lambda: drive.files().get(
        fileId=file_id,
        fields=_FILE_FIELDS + ",description,sharingUser,owners",
    ).execute())
    meta = _normalize_file(result)
    meta["owners"]      = [o.get("displayName", "") for o in result.get("owners", [])]
    meta["description"] = result.get("description", "")
    return meta


def get_storage_info() -> dict:
    """Return Drive storage quota: used, total, and free space (in bytes)."""
    drive  = authenticate_drive()
    result = _api_call(lambda: drive.about().get(fields="storageQuota").execute())
    quota  = result.get("storageQuota", {})
    limit  = int(quota.get("limit", 0))
    used   = int(quota.get("usage", 0))
    free   = limit - used if limit else 0
    def _fmt_gb(b: int) -> str:
        return f"{b / 1_073_741_824:.2f} GB"
    return {
        "total_bytes":  limit,
        "used_bytes":   used,
        "free_bytes":   free,
        "total":        _fmt_gb(limit) if limit else "Unlimited",
        "used":         _fmt_gb(used),
        "free":         _fmt_gb(free) if limit else "N/A",
        "percent_used": f"{used / limit * 100:.1f}%" if limit else "N/A",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────────────────────

def create_folder(name: str, parent_id: str = "") -> dict:
    """Create a new folder in Drive (optionally inside another folder)."""
    drive = authenticate_drive()
    body: dict = {"name": name, "mimeType": _FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    result = _api_call(lambda: drive.files().create(
        body=body,
        fields="id,name,mimeType,webViewLink",
    ).execute())
    return {
        "id":      result.get("id", ""),
        "name":    result.get("name", name),
        "type":    "Folder",
        "url":     result.get("webViewLink", ""),
        "message": f"Folder '{name}' created successfully.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Organise
# ─────────────────────────────────────────────────────────────────────────────

def rename_file(file_id: str, new_name: str) -> dict:
    """Rename a file or folder."""
    drive  = authenticate_drive()
    result = _api_call(lambda: drive.files().update(
        fileId=file_id,
        body={"name": new_name},
        fields="id,name",
    ).execute())
    return {
        "success": True,
        "id":      result.get("id", file_id),
        "name":    result.get("name", new_name),
        "message": f"Renamed to '{new_name}'.",
    }


def move_file(file_id: str, destination_folder_id: str) -> dict:
    """Move a file into a different folder."""
    drive = authenticate_drive()
    # Fetch current parents so we can remove them
    current = _api_call(lambda: drive.files().get(
        fileId=file_id, fields="parents"
    ).execute())
    old_parents = ",".join(current.get("parents", []))
    result = _api_call(lambda: drive.files().update(
        fileId=file_id,
        addParents=destination_folder_id,
        removeParents=old_parents,
        fields="id,name,parents",
    ).execute())
    return {
        "success": True,
        "id":      result.get("id", file_id),
        "parents": result.get("parents", []),
        "message": f"File moved to folder '{destination_folder_id}'.",
    }


def copy_file(file_id: str, new_name: str = "", destination_folder_id: str = "") -> dict:
    """Copy a file, optionally with a new name and/or into a different folder."""
    drive = authenticate_drive()
    body: dict = {}
    if new_name:
        body["name"] = new_name
    if destination_folder_id:
        body["parents"] = [destination_folder_id]
    result = _api_call(lambda: drive.files().copy(
        fileId=file_id,
        body=body,
        fields="id,name,webViewLink",
    ).execute())
    return {
        "success": True,
        "id":      result.get("id", ""),
        "name":    result.get("name", "Copy"),
        "url":     result.get("webViewLink", ""),
        "message": f"File copied as '{result.get('name', 'Copy')}'.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Trash / Delete / Restore
# ─────────────────────────────────────────────────────────────────────────────

def trash_file(file_id: str) -> dict:
    """Move a file or folder to the Drive trash."""
    drive  = authenticate_drive()
    _api_call(lambda: drive.files().update(
        fileId=file_id, body={"trashed": True}
    ).execute())
    return {"success": True, "message": f"File {file_id} moved to trash."}


def restore_file(file_id: str) -> dict:
    """Restore a file or folder from the Drive trash."""
    drive  = authenticate_drive()
    _api_call(lambda: drive.files().update(
        fileId=file_id, body={"trashed": False}
    ).execute())
    return {"success": True, "message": f"File {file_id} restored from trash."}


def delete_file(file_id: str) -> dict:
    """Permanently delete a file or folder (cannot be undone)."""
    drive = authenticate_drive()
    _api_call(lambda: drive.files().delete(fileId=file_id).execute())
    return {"success": True, "message": f"File {file_id} permanently deleted."}


# ─────────────────────────────────────────────────────────────────────────────
# Sharing & Permissions
# ─────────────────────────────────────────────────────────────────────────────

def get_file_permissions(file_id: str) -> list[dict]:
    """List all sharing permissions for a file or folder."""
    drive   = authenticate_drive()
    results = _api_call(lambda: drive.permissions().list(
        fileId=file_id,
        fields="permissions(id,type,role,emailAddress,displayName)",
    ).execute())
    perms = []
    for p in results.get("permissions", []):
        perms.append({
            "id":      p.get("id", ""),
            "type":    p.get("type", ""),
            "role":    p.get("role", ""),
            "email":   p.get("emailAddress", ""),
            "name":    p.get("displayName", ""),
        })
    return perms


def share_file(file_id: str, email: str, role: str = "reader") -> dict:
    """
    Share a file with a specific user.
    role: 'reader' | 'commenter' | 'writer' | 'fileOrganizer' | 'organizer' | 'owner'
    """
    valid_roles = {"reader", "commenter", "writer", "fileOrganizer", "organizer", "owner"}
    role = role.lower()
    if role not in valid_roles:
        role = "reader"
    drive = authenticate_drive()
    result = _api_call(lambda: drive.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": role, "emailAddress": email},
        sendNotificationEmail=True,
        fields="id,role,emailAddress",
    ).execute())
    return {
        "success":       True,
        "permission_id": result.get("id", ""),
        "role":          result.get("role", role),
        "email":         result.get("emailAddress", email),
        "message":       f"Shared with {email} as {role}.",
    }


def share_file_publicly(file_id: str, role: str = "reader") -> dict:
    """Make a file accessible to anyone with the link."""
    valid_roles = {"reader", "commenter", "writer"}
    role = role.lower() if role.lower() in valid_roles else "reader"
    drive = authenticate_drive()
    _api_call(lambda: drive.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": role},
        fields="id",
    ).execute())
    # Fetch the shareable link
    meta = _api_call(lambda: drive.files().get(
        fileId=file_id, fields="webViewLink"
    ).execute())
    return {
        "success": True,
        "role":    role,
        "url":     meta.get("webViewLink", ""),
        "message": f"File is now accessible to anyone with the link ({role}).",
    }


def get_shareable_link(file_id: str) -> dict:
    """Get the shareable web link for a file (makes it public if not already)."""
    return share_file_publicly(file_id, role="reader")


def remove_permission(file_id: str, permission_id: str) -> dict:
    """Remove a specific sharing permission from a file."""
    drive = authenticate_drive()
    _api_call(lambda: drive.permissions().delete(
        fileId=file_id, permissionId=permission_id
    ).execute())
    return {"success": True, "message": f"Permission {permission_id} removed."}


def remove_access(file_id: str, email: str) -> dict:
    """Remove access for a specific email address (finds and deletes their permission)."""
    perms = get_file_permissions(file_id)
    target = next((p for p in perms if p.get("email", "").lower() == email.lower()), None)
    if not target:
        return {"success": False, "error": f"No permission found for {email}."}
    return remove_permission(file_id, target["id"])


def make_file_private(file_id: str) -> dict:
    """Remove all public / anyone-with-link sharing from a file."""
    perms = get_file_permissions(file_id)
    removed = 0
    for p in perms:
        if p.get("type") in ("anyone", "domain"):
            remove_permission(file_id, p["id"])
            removed += 1
    if removed:
        return {"success": True, "message": f"Removed {removed} public permission(s). File is now private."}
    return {"success": True, "message": "File was already private."}


def upload_file(
    file_path: str,
    folder_id: Optional[str] = None,
    file_name: Optional[str] = None,
) -> dict:
    """
    Upload a local file to Google Drive.

    Parameters
    ----------
    file_path : str
        Absolute path to the local file to upload.
    folder_id : str, optional
        Drive folder ID to upload into.  Defaults to Drive root.
    file_name : str, optional
        Name to give the uploaded file.  Defaults to the local file name.
    """
    import mimetypes
    from pathlib import Path as _Path
    from googleapiclient.http import MediaFileUpload

    fpath = _Path(file_path)
    if not fpath.exists():
        return {"success": False, "error": f"File not found: {file_path}"}

    name = file_name or fpath.name
    mime, _ = mimetypes.guess_type(str(fpath))
    mime = mime or "application/octet-stream"

    metadata: dict = {"name": name}
    if folder_id:
        metadata["parents"] = [folder_id]

    try:
        drive   = authenticate_drive()
        media   = MediaFileUpload(str(fpath), mimetype=mime, resumable=False)
        uploaded = _api_call(lambda: drive.files().create(
            body=metadata,
            media_body=media,
            fields="id,name,mimeType,webViewLink,size,parents,modifiedTime,starred,trashed",
        ).execute())
        return {"success": True, "result": _normalize_file(uploaded)}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
