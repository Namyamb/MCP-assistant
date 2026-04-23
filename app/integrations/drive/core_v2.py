"""
Google Drive MCP v2 — Agent-Native, Production-Ready Integration

Upgrades from v1:
  • Name→ID resolution layer (accepts file_id OR file_name)
  • Pagination support with next_page_token
  • Permission safety layer (prevents dangerous operations)
  • Batch operations (delete_files, move_files, copy_files)
  • Advanced search with multiple filters
  • Context-friendly standardized responses
  • File-type intelligence with MCP suggestions
  • Lightweight caching with TTL
  • Structured error handling (custom exceptions)
  • Structured logging and observability

Maintains full backward compatibility with existing tool signatures.
"""

from __future__ import annotations

import time
import threading
from typing import Optional
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

from app.core.config import TOKEN_FILE, ALL_SCOPES

# Import utilities from new utils module
from app.integrations.drive.utils import (
    # Errors
    DriveError,
    DriveNotFoundError, DrivePermissionError, DriveRateLimitError,
    DriveValidationError, DriveAmbiguityError,
    # Cache
    cached, invalidate_cache, _drive_cache,
    # Logging
    log_tool_call, _drive_logger,
    # Validation
    validate_email, is_dangerous_role, sanitize_drive_query,
    # Response helpers
    standardize_file_response, get_mcp_suggestion, paginated_response,
)

# ═════════════════════════════════════════════════════════════════════════════
# THREAD-LOCAL STATE
# ═════════════════════════════════════════════════════════════════════════════

_thread_local = threading.local()


# ═════════════════════════════════════════════════════════════════════════════
# AUTHENTICATION
# ═════════════════════════════════════════════════════════════════════════════

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


def _reset_service() -> None:
    """Reset the thread-local Drive service."""
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


# ═════════════════════════════════════════════════════════════════════════════
# RETRY WRAPPER WITH PROPER ERROR HANDLING
# ═════════════════════════════════════════════════════════════════════════════

def _api_call(fn, retries: int = 3, backoff: float = 1.5):
    """Execute API call with retry logic and proper error translation."""
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
            
            if status == 404:
                raise DriveNotFoundError()
            
            if status == 403:
                raise DrivePermissionError(reason=str(exc))
            
            if status == 429:
                retry_after = int(exc.resp.get("retry-after", 60))
                raise DriveRateLimitError(retry_after=retry_after)
            
            if status in (500, 502, 503, 504):
                last_err = exc
                if attempt < retries - 1:
                    time.sleep(backoff * (attempt + 1))
                continue
            
            raise DriveError(f"Drive API error ({status}): {exc}")
            
        except (OSError, ConnectionError) as exc:
            last_err = exc
            if attempt < retries - 1:
                time.sleep(backoff * (attempt + 1))
    
    raise DriveError(f"Drive API call failed after {retries} attempts: {last_err}")


# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

_FOLDER_MIME = "application/vnd.google-apps.folder"

_FILE_FIELDS = (
    "id,name,mimeType,modifiedTime,size,parents,webViewLink,"
    "webContentLink,starred,trashed,description,owners,createdTime"
)

_FULL_FILE_FIELDS = _FILE_FIELDS + ",permissions,shared,viewedByMeTime"

_TYPE_MAP = {
    "doc": "application/vnd.google-apps.document",
    "docs": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "sheets": "application/vnd.google-apps.spreadsheet",
    "slides": "application/vnd.google-apps.presentation",
    "pdf": "application/pdf",
    "image": "image/",
    "folder": _FOLDER_MIME,
    "video": "video/",
    "audio": "audio/",
}


# ═════════════════════════════════════════════════════════════════════════════
# ID RESOLUTION LAYER (Name → ID)
# ═════════════════════════════════════════════════════════════════════════════

def resolve_file_id(
    name_or_id: str,
    allow_ambiguity: bool = False,
    folder_id: str = ""
) -> str:
    """
    Resolve a file name or ID to a file ID.
    
    Args:
        name_or_id: File name or ID to resolve
        allow_ambiguity: If True, returns first match; if False, raises on multiple matches
        folder_id: Optional parent folder to narrow search
    
    Returns:
        File ID string
    
    Raises:
        DriveNotFoundError: If no file found
        DriveAmbiguityError: If multiple matches and allow_ambiguity=False
    """
    # If it looks like an ID (alphanumeric, ~33 chars), assume it's an ID
    if len(name_or_id) >= 25 and name_or_id.replace("-", "").replace("_", "").isalnum():
        return name_or_id
    
    drive = authenticate_drive()
    safe_name = sanitize_drive_query(name_or_id)
    
    # Build query
    query_parts = [f"name contains '{safe_name}'", "trashed=false"]
    query_parts.append(f"mimeType != '{_FOLDER_MIME}'")  # Exclude folders
    if folder_id:
        query_parts.append(f"'{folder_id}' in parents")
    
    query = " and ".join(query_parts)
    
    results = _api_call(lambda: drive.files().list(
        q=query,
        pageSize=10,
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    
    files = results.get("files", [])
    
    if not files:
        raise DriveNotFoundError(resource_id=name_or_id, resource_type="file")
    
    # Check for exact name match
    exact_matches = [f for f in files if f.get("name", "").lower() == name_or_id.lower()]
    if len(exact_matches) == 1:
        return exact_matches[0]["id"]
    
    if len(files) > 1 and not allow_ambiguity:
        raise DriveAmbiguityError(
            name=name_or_id,
            matches=[{"id": f["id"], "name": f["name"]} for f in files[:5]]
        )
    
    # Return best match (first by recency)
    return files[0]["id"]


def resolve_folder_id(
    name_or_id: str,
    allow_ambiguity: bool = False
) -> str:
    """
    Resolve a folder name or ID to a folder ID.
    
    Args:
        name_or_id: Folder name or ID to resolve
        allow_ambiguity: If True, returns first match; if False, raises on multiple matches
    
    Returns:
        Folder ID string
    
    Raises:
        DriveNotFoundError: If no folder found
        DriveAmbiguityError: If multiple matches and allow_ambiguity=False
    """
    # If it looks like an ID, assume it's an ID
    if len(name_or_id) >= 25 and name_or_id.replace("-", "").replace("_", "").isalnum():
        return name_or_id
    
    drive = authenticate_drive()
    safe_name = sanitize_drive_query(name_or_id)
    
    # Try exact match first
    exact_query = (
        f"name='{safe_name}' and mimeType='{_FOLDER_MIME}' and trashed=false"
    )
    
    results = _api_call(lambda: drive.files().list(
        q=exact_query,
        pageSize=5,
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    
    folders = results.get("files", [])
    
    if len(folders) == 1:
        return folders[0]["id"]
    
    # Fallback to partial match
    partial_query = (
        f"name contains '{safe_name.lower()}' and mimeType='{_FOLDER_MIME}' and trashed=false"
    )
    
    results2 = _api_call(lambda: drive.files().list(
        q=partial_query,
        pageSize=5,
        orderBy="modifiedTime desc",
        fields=f"files({_FILE_FIELDS})",
    ).execute())
    
    folders = results2.get("files", [])
    
    if not folders:
        raise DriveNotFoundError(resource_id=name_or_id, resource_type="folder")
    
    if len(folders) > 1 and not allow_ambiguity:
        raise DriveAmbiguityError(
            name=name_or_id,
            matches=[{"id": f["id"], "name": f["name"]} for f in folders[:5]]
        )
    
    return folders[0]["id"]


def _resolve_id(
    name_or_id: str,
    item_type: str = "file",
    allow_ambiguity: bool = True,
    folder_id: str = ""
) -> str:
    """
    Unified ID resolver that handles both files and folders.
    
    Args:
        name_or_id: Name or ID to resolve
        item_type: "file" or "folder"
        allow_ambiguity: Whether to allow multiple matches
        folder_id: Optional parent folder for files
    
    Returns:
        Resolved ID
    """
    if item_type == "folder":
        return resolve_folder_id(name_or_id, allow_ambiguity)
    return resolve_file_id(name_or_id, allow_ambiguity, folder_id)


# ═════════════════════════════════════════════════════════════════════════════
# LIST / BROWSE (WITH PAGINATION)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def list_files(
    limit: int = 10,
    folder_id: str = "",
    page_token: str = ""
) -> dict:
    """
    List files in Drive with pagination support.
    
    Args:
        limit: Maximum files to return (max 100)
        folder_id: Optional parent folder ID
        page_token: Token for next page (from previous response)
        use_cache: Whether to use cached results
    
    Returns:
        {
            "files": [standardized_file, ...],
            "next_page_token": "..." or None,
            "total_count": int,
            "has_more": bool
        }
    """
    drive = authenticate_drive()
    query = "trashed=false"
    
    if folder_id:
        folder_id_resolved = _resolve_id(folder_id, "folder", allow_ambiguity=True)
        query += f" and '{folder_id_resolved}' in parents"
    
    params = {
        "q": query,
        "pageSize": min(limit, 100),
        "orderBy": "modifiedTime desc",
        "fields": f"nextPageToken,files({_FILE_FIELDS})",
    }
    
    if page_token:
        params["pageToken"] = page_token
    
    results = _api_call(lambda: drive.files().list(**params).execute())
    
    files = [standardize_file_response(f) for f in results.get("files", [])]
    next_token = results.get("nextPageToken")
    
    return paginated_response(files, next_token)


@log_tool_call
def list_folders(
    limit: int = 20,
    page_token: str = ""
) -> dict:
    """List folders with pagination support."""
    drive = authenticate_drive()
    
    params = {
        "q": f"mimeType='{_FOLDER_MIME}' and trashed=false",
        "pageSize": min(limit, 100),
        "orderBy": "modifiedTime desc",
        "fields": f"nextPageToken,files({_FILE_FIELDS})",
    }
    
    if page_token:
        params["pageToken"] = page_token
    
    results = _api_call(lambda: drive.files().list(**params).execute())
    
    folders = [standardize_file_response(f) for f in results.get("files", [])]
    next_token = results.get("nextPageToken")
    
    return paginated_response(folders, next_token)


@log_tool_call
def get_folder_contents(
    folder_id: str,
    limit: int = 20,
    page_token: str = ""
) -> dict:
    """Get contents of a folder with pagination."""
    resolved_id = _resolve_id(folder_id, "folder", allow_ambiguity=True)
    return list_files(limit=limit, folder_id=resolved_id, page_token=page_token)


@log_tool_call
def get_starred_files(
    limit: int = 20,
    page_token: str = ""
) -> dict:
    """List starred files with pagination."""
    drive = authenticate_drive()
    
    params = {
        "q": "starred=true and trashed=false",
        "pageSize": min(limit, 100),
        "orderBy": "modifiedTime desc",
        "fields": f"nextPageToken,files({_FILE_FIELDS})",
    }
    
    if page_token:
        params["pageToken"] = page_token
    
    results = _api_call(lambda: drive.files().list(**params).execute())
    
    files = [standardize_file_response(f) for f in results.get("files", [])]
    next_token = results.get("nextPageToken")
    
    return paginated_response(files, next_token)


@log_tool_call
def get_recent_files(limit: int = 10) -> dict:
    """Get recently modified files."""
    return list_files(limit=limit)


# ═════════════════════════════════════════════════════════════════════════════
# ADVANCED SEARCH
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def search_files(
    query: str = "",
    file_type: str = "",
    date_from: str = "",
    date_to: str = "",
    owner: str = "",
    folder_id: str = "",
    starred_only: bool = False,
    trashed: bool = False,
    limit: int = 10,
    page_token: str = ""
) -> dict:
    """
    Advanced file search with multiple filter options.
    
    Args:
        query: Text to search in name or content
        file_type: Type filter (doc, sheet, pdf, image, etc.)
        date_from: ISO date string (e.g., "2024-01-01")
        date_to: ISO date string (e.g., "2024-12-31")
        owner: Email of file owner
        folder_id: Search within specific folder
        starred_only: Only return starred files
        trashed: Include trashed files (default: False)
        limit: Maximum results
        page_token: Pagination token
    
    Returns:
        Paginated response with matching files
    """
    drive = authenticate_drive()
    query_parts = []
    
    # Text search
    if query:
        safe_query = sanitize_drive_query(query)
        query_parts.append(f"(name contains '{safe_query}' or fullText contains '{safe_query}')")
    
    # File type filter
    if file_type:
        mime = _TYPE_MAP.get(file_type.lower(), "")
        if mime:
            if mime.endswith("/"):
                query_parts.append(f"mimeType contains '{mime}'")
            else:
                query_parts.append(f"mimeType='{mime}'")
    
    # Date range
    if date_from:
        query_parts.append(f"modifiedTime >= '{date_from}T00:00:00'")
    if date_to:
        query_parts.append(f"modifiedTime <= '{date_to}T23:59:59'")
    
    # Owner
    if owner:
        if validate_email(owner):
            query_parts.append(f"'{owner}' in owners")
    
    # Folder constraint
    if folder_id:
        resolved_folder = _resolve_id(folder_id, "folder", allow_ambiguity=True)
        query_parts.append(f"'{resolved_folder}' in parents")
    
    # Starred
    if starred_only:
        query_parts.append("starred=true")
    
    # Trash status
    if not trashed:
        query_parts.append("trashed=false")
    
    # Combine all conditions
    drive_query = " and ".join(query_parts) if query_parts else "trashed=false"
    
    params = {
        "q": drive_query,
        "pageSize": min(limit, 100),
        "orderBy": "modifiedTime desc",
        "fields": f"nextPageToken,files({_FILE_FIELDS})",
    }
    
    if page_token:
        params["pageToken"] = page_token
    
    results = _api_call(lambda: drive.files().list(**params).execute())
    
    files = [standardize_file_response(f) for f in results.get("files", [])]
    next_token = results.get("nextPageToken")
    
    return paginated_response(files, next_token)


@log_tool_call
def search_files_by_type(
    file_type: str,
    limit: int = 10,
    page_token: str = ""
) -> dict:
    """Search files by type (backward compatible with pagination)."""
    return search_files(file_type=file_type, limit=limit, page_token=page_token)


# ═════════════════════════════════════════════════════════════════════════════
# METADATA
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
@cached(ttl=60, key_prefix="metadata")
def get_file_metadata(file_id: str) -> dict:
    """
    Get comprehensive file metadata with MCP suggestions.
    
    Args:
        file_id: File ID or name
    
    Returns:
        Standardized metadata with additional fields:
        - owners: List of owner names
        - description: File description
        - suggested_mcp: Recommended MCP for this file type
    """
    resolved_id = _resolve_id(file_id, "file", allow_ambiguity=True)
    drive = authenticate_drive()
    
    result = _api_call(lambda: drive.files().get(
        fileId=resolved_id,
        fields=_FULL_FILE_FIELDS,
    ).execute())
    
    meta = standardize_file_response(result)
    meta.update({
        "owners": [o.get("displayName", "") for o in result.get("owners", [])],
        "description": result.get("description", ""),
        "shared": result.get("shared", False),
        "viewed_by_me": result.get("viewedByMeTime", ""),
        "created_time": result.get("createdTime", ""),
        "suggested_mcp": get_mcp_suggestion(result.get("mimeType", "")),
        "is_google_workspace_file": result.get("mimeType", "").startswith(
            "application/vnd.google-apps."),
    })
    
    return meta


@log_tool_call
def get_storage_info() -> dict:
    """Return Drive storage quota information."""
    drive = authenticate_drive()
    result = _api_call(lambda: drive.about().get(fields="storageQuota").execute())
    
    quota = result.get("storageQuota", {})
    limit = int(quota.get("limit", 0))
    usage = int(quota.get("usage", 0))
    usage_in_drive = int(quota.get("usageInDrive", 0))
    usage_in_trash = int(quota.get("usageInDriveTrash", 0))
    free = limit - usage if limit else 0
    
    def _fmt_gb(b: int) -> str:
        return f"{b / 1_073_741_824:.2f} GB"
    
    return {
        "total_bytes": limit,
        "used_bytes": usage,
        "free_bytes": free,
        "used_in_drive": usage_in_drive,
        "used_in_trash": usage_in_trash,
        "total": _fmt_gb(limit) if limit else "Unlimited",
        "used": _fmt_gb(usage),
        "free": _fmt_gb(free) if limit else "N/A",
        "percent_used": f"{usage / limit * 100:.1f}%" if limit else "N/A",
    }


# ═════════════════════════════════════════════════════════════════════════════
# CREATE
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def create_folder(
    name: str,
    parent_id: str = "",
    parent_name: str = ""
) -> dict:
    """
    Create a new folder.
    
    Args:
        name: Folder name
        parent_id: Parent folder ID (optional)
        parent_name: Parent folder name (alternative to parent_id)
    
    Returns:
        Standardized folder metadata
    """
    drive = authenticate_drive()
    
    body: dict = {"name": name, "mimeType": _FOLDER_MIME}
    
    # Resolve parent
    if parent_id:
        body["parents"] = [parent_id]
    elif parent_name:
        resolved_parent = _resolve_id(parent_name, "folder", allow_ambiguity=True)
        body["parents"] = [resolved_parent]
    
    result = _api_call(lambda: drive.files().create(
        body=body,
        fields=_FILE_FIELDS,
    ).execute())
    
    # Invalidate cache
    invalidate_cache("list_folders")
    
    return standardize_file_response(result)


# ═════════════════════════════════════════════════════════════════════════════
# ORGANIZE (WITH NAME RESOLUTION)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def rename_file(
    file_id: str = "",
    file_name: str = "",
    new_name: str = ""
) -> dict:
    """
    Rename a file or folder.
    
    Args:
        file_id: File ID (preferred)
        file_name: File name (will be resolved to ID)
        new_name: New name for the file
    
    Returns:
        Updated file metadata
    """
    if not new_name:
        raise DriveValidationError("new_name", new_name, "cannot be empty")
    
    # Resolve ID
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    result = _api_call(lambda: drive.files().update(
        fileId=resolved_id,
        body={"name": new_name},
        fields=_FILE_FIELDS,
    ).execute())
    
    invalidate_cache("metadata")
    
    return standardize_file_response(result)


@log_tool_call
def move_file(
    file_id: str = "",
    file_name: str = "",
    destination_folder_id: str = "",
    destination_folder_name: str = ""
) -> dict:
    """
    Move a file to a different folder.
    
    Args:
        file_id: File ID (preferred)
        file_name: File name (alternative)
        destination_folder_id: Destination folder ID
        destination_folder_name: Destination folder name (alternative)
    
    Returns:
        Updated file metadata
    """
    # Resolve file ID
    resolved_file_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    # Resolve destination folder
    if destination_folder_id:
        resolved_dest_id = destination_folder_id
    elif destination_folder_name:
        resolved_dest_id = _resolve_id(destination_folder_name, "folder", allow_ambiguity=True)
    else:
        raise DriveValidationError("destination", "", "Either destination_folder_id or destination_folder_name required")
    
    drive = authenticate_drive()
    
    # Fetch current parents
    current = _api_call(lambda: drive.files().get(
        fileId=resolved_file_id, fields="parents"
    ).execute())
    old_parents = ",".join(current.get("parents", []))
    
    result = _api_call(lambda: drive.files().update(
        fileId=resolved_file_id,
        addParents=resolved_dest_id,
        removeParents=old_parents,
        fields=_FILE_FIELDS,
    ).execute())
    
    invalidate_cache("metadata")
    invalidate_cache("list_files")
    
    return standardize_file_response(result)


@log_tool_call
def copy_file(
    file_id: str = "",
    file_name: str = "",
    new_name: str = "",
    destination_folder_id: str = "",
    destination_folder_name: str = ""
) -> dict:
    """
    Copy a file.
    
    Args:
        file_id: Source file ID
        file_name: Source file name (alternative)
        new_name: Name for the copy
        destination_folder_id: Destination folder ID
        destination_folder_name: Destination folder name (alternative)
    
    Returns:
        New file metadata
    """
    # Resolve source
    resolved_file_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    # Resolve destination
    resolved_dest_id = ""
    if destination_folder_id:
        resolved_dest_id = destination_folder_id
    elif destination_folder_name:
        resolved_dest_id = _resolve_id(destination_folder_name, "folder", allow_ambiguity=True)
    
    drive = authenticate_drive()
    
    body: dict = {}
    if new_name:
        body["name"] = new_name
    if resolved_dest_id:
        body["parents"] = [resolved_dest_id]
    
    result = _api_call(lambda: drive.files().copy(
        fileId=resolved_file_id,
        body=body,
        fields=_FILE_FIELDS,
    ).execute())
    
    invalidate_cache("list_files")
    
    return standardize_file_response(result)


# ═════════════════════════════════════════════════════════════════════════════
# TRASH / DELETE / RESTORE
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def trash_file(file_id: str = "", file_name: str = "") -> dict:
    """Move a file to trash."""
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    result = _api_call(lambda: drive.files().update(
        fileId=resolved_id,
        body={"trashed": True},
        fields=_FILE_FIELDS,
    ).execute())
    
    invalidate_cache("metadata")
    invalidate_cache("list_files")
    
    return {
        "success": True,
        "file": standardize_file_response(result),
        "message": f"File moved to trash.",
    }


@log_tool_call
def restore_file(file_id: str = "", file_name: str = "") -> dict:
    """Restore a file from trash."""
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    result = _api_call(lambda: drive.files().update(
        fileId=resolved_id,
        body={"trashed": False},
        fields=_FILE_FIELDS,
    ).execute())
    
    invalidate_cache("list_files")
    
    return {
        "success": True,
        "file": standardize_file_response(result),
        "message": f"File restored from trash.",
    }


@log_tool_call
def delete_file(file_id: str = "", file_name: str = "") -> dict:
    """Permanently delete a file."""
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    _api_call(lambda: drive.files().delete(fileId=resolved_id).execute())
    
    invalidate_cache("metadata")
    invalidate_cache("list_files")
    
    return {
        "success": True,
        "deleted_id": resolved_id,
        "message": "File permanently deleted.",
    }


# Folder aliases for intuitive API (folders are files in Drive)
def trash_folder(folder_id: str = "", folder_name: str = "") -> dict:
    """
    Move a folder to trash.
    
    This is an alias for trash_file - folders are technically files in Google Drive.
    """
    resolved_id = folder_id if folder_id else resolve_folder_id(folder_name, allow_ambiguity=True)
    result = trash_file(file_id=resolved_id)
    result["message"] = result["message"].replace("File", "Folder")
    return result


def delete_folder(folder_id: str = "", folder_name: str = "") -> dict:
    """
    Permanently delete a folder.
    
    This is an alias for delete_file - folders are technically files in Google Drive.
    """
    resolved_id = folder_id if folder_id else resolve_folder_id(folder_name, allow_ambiguity=True)
    result = delete_file(file_id=resolved_id)
    result["message"] = result["message"].replace("File", "Folder")
    return result


def restore_folder(folder_id: str = "", folder_name: str = "") -> dict:
    """
    Restore a folder from trash.
    
    This is an alias for restore_file - folders are technically files in Google Drive.
    """
    resolved_id = folder_id if folder_id else resolve_folder_id(folder_name, allow_ambiguity=True)
    result = restore_file(file_id=resolved_id)
    result["message"] = result["message"].replace("File", "Folder")
    return result


# ═════════════════════════════════════════════════════════════════════════════
# BATCH OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def delete_files(
    file_ids: list[str] = None,
    file_names: list[str] = None,
    require_confirmation: bool = True
) -> dict:
    """
    Delete multiple files in batch.

    Args:
        file_ids: List of file IDs to delete
        file_names: List of file names to delete (will be resolved)
        require_confirmation: If True, raises DrivePermissionError for >5 files

    Returns:
        Summary of deletions
    """
    file_ids = file_ids or []
    file_names = file_names or []

    # Resolve names to IDs
    ids_to_delete = list(file_ids)
    for name in file_names:
        try:
            resolved_id = _resolve_id(name, "file", allow_ambiguity=True)
            ids_to_delete.append(resolved_id)
        except DriveNotFoundError:
            pass  # Skip not found

    # Safety gate: permanent deletion is irreversible
    if require_confirmation and len(ids_to_delete) > 5:
        raise DrivePermissionError(
            action="delete_files",
            reason=(
                f"Permanently deleting {len(ids_to_delete)} files requires explicit confirmation. "
                "Set require_confirmation=False to proceed. This action cannot be undone."
            )
        )

    results = {"success": [], "failed": []}

    for fid in ids_to_delete:
        try:
            delete_file(file_id=fid)
            results["success"].append(fid)
        except Exception as e:
            results["failed"].append({"id": fid, "error": str(e)})

    invalidate_cache("list_files")

    return {
        "success": True,
        "deleted_count": len(results["success"]),
        "failed_count": len(results["failed"]),
        "results": results,
    }


@log_tool_call
def move_files(
    file_ids: list[str] = None,
    file_names: list[str] = None,
    destination_folder_id: str = "",
    destination_folder_name: str = ""
) -> dict:
    """
    Move multiple files to a folder.
    
    Args:
        file_ids: List of file IDs
        file_names: List of file names (alternative)
        destination_folder_id: Target folder ID
        destination_folder_name: Target folder name (alternative)
    
    Returns:
        Summary of moves
    """
    file_ids = file_ids or []
    file_names = file_names or []
    
    # Resolve destination
    dest_id = destination_folder_id
    if destination_folder_name:
        dest_id = _resolve_id(destination_folder_name, "folder", allow_ambiguity=True)
    
    if not dest_id:
        raise DriveValidationError("destination", "", "Destination folder required")
    
    # Collect all file IDs
    ids_to_move = list(file_ids)
    for name in file_names:
        try:
            resolved_id = _resolve_id(name, "file", allow_ambiguity=True)
            ids_to_move.append(resolved_id)
        except DriveNotFoundError:
            pass
    
    results = {"success": [], "failed": []}
    
    for fid in ids_to_move:
        try:
            move_file(file_id=fid, destination_folder_id=dest_id)
            results["success"].append(fid)
        except Exception as e:
            results["failed"].append({"id": fid, "error": str(e)})
    
    return {
        "success": True,
        "moved_count": len(results["success"]),
        "failed_count": len(results["failed"]),
        "destination": dest_id,
        "results": results,
    }


@log_tool_call
def copy_files(
    file_ids: list[str] = None,
    file_names: list[str] = None,
    destination_folder_id: str = "",
    destination_folder_name: str = ""
) -> dict:
    """
    Copy multiple files.
    
    Args:
        file_ids: List of file IDs
        file_names: List of file names (alternative)
        destination_folder_id: Target folder ID
        destination_folder_name: Target folder name (alternative)
    
    Returns:
        Summary of copies with new IDs
    """
    file_ids = file_ids or []
    file_names = file_names or []
    
    # Resolve destination
    dest_id = destination_folder_id
    if destination_folder_name:
        dest_id = _resolve_id(destination_folder_name, "folder", allow_ambiguity=True)
    
    # Collect all file IDs
    ids_to_copy = list(file_ids)
    for name in file_names:
        try:
            resolved_id = _resolve_id(name, "file", allow_ambiguity=True)
            ids_to_copy.append(resolved_id)
        except DriveNotFoundError:
            pass
    
    results = {"success": [], "failed": []}
    
    for fid in ids_to_copy:
        try:
            copied = copy_file(file_id=fid, destination_folder_id=dest_id)
            results["success"].append({"original": fid, "copy": copied})
        except Exception as e:
            results["failed"].append({"id": fid, "error": str(e)})
    
    return {
        "success": True,
        "copied_count": len(results["success"]),
        "failed_count": len(results["failed"]),
        "destination": dest_id,
        "results": results,
    }


# ═════════════════════════════════════════════════════════════════════════════
# SHARING & PERMISSIONS (WITH SAFETY LAYER)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def get_file_permissions(file_id: str = "", file_name: str = "") -> list[dict]:
    """List all sharing permissions for a file."""
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    results = _api_call(lambda: drive.permissions().list(
        fileId=resolved_id,
        fields="permissions(id,type,role,emailAddress,displayName)",
    ).execute())
    
    perms = []
    for p in results.get("permissions", []):
        perms.append({
            "id": p.get("id", ""),
            "type": p.get("type", ""),  # user, group, domain, anyone
            "role": p.get("role", ""),  # reader, commenter, writer, owner
            "email": p.get("emailAddress", ""),
            "name": p.get("displayName", ""),
        })
    
    return perms


@log_tool_call
def share_file(
    file_id: str = "",
    file_name: str = "",
    email: str = "",
    role: str = "reader",
    notify: bool = True
) -> dict:
    """
    Share a file with a specific user.
    
    Args:
        file_id: File ID
        file_name: File name (alternative)
        email: Email of user to share with
        role: Permission level (reader/commenter/writer)
        notify: Send notification email
    
    Returns:
        Permission details
    """
    # Validation
    if not email:
        raise DriveValidationError("email", email, "required")
    
    if not validate_email(email):
        raise DriveValidationError("email", email, "invalid format")
    
    # Safety check for dangerous roles
    is_dangerous, reason = is_dangerous_role(role)
    if is_dangerous:
        raise DrivePermissionError(action="share", reason=reason)
    
    # Validate role
    valid_roles = {"reader", "commenter", "writer"}
    role = role.lower()
    if role not in valid_roles:
        role = "reader"
    
    # Resolve file
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    result = _api_call(lambda: drive.permissions().create(
        fileId=resolved_id,
        body={"type": "user", "role": role, "emailAddress": email},
        sendNotificationEmail=notify,
        fields="id,role,emailAddress",
    ).execute())
    
    return {
        "success": True,
        "permission_id": result.get("id", ""),
        "role": result.get("role", role),
        "email": result.get("emailAddress", email),
        "file_id": resolved_id,
        "message": f"Shared with {email} as {role}.",
    }


@log_tool_call
def share_file_publicly(
    file_id: str = "",
    file_name: str = "",
    role: str = "reader"
) -> dict:
    """
    Make a file accessible to anyone with the link.
    
    Args:
        file_id: File ID
        file_name: File name (alternative)
        role: Permission level (reader/commenter/writer only)
    
    Returns:
        Shareable link and details
    """
    # Safety: limit roles for public sharing
    valid_roles = {"reader", "commenter", "writer"}
    role = role.lower() if role.lower() in valid_roles else "reader"
    
    # Resolve file
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    _api_call(lambda: drive.permissions().create(
        fileId=resolved_id,
        body={"type": "anyone", "role": role},
        fields="id",
    ).execute())
    
    # Fetch the shareable link
    meta = _api_call(lambda: drive.files().get(
        fileId=resolved_id, fields="webViewLink"
    ).execute())
    
    return {
        "success": True,
        "role": role,
        "url": meta.get("webViewLink", ""),
        "file_id": resolved_id,
        "message": f"File is now accessible to anyone with the link ({role}).",
    }


@log_tool_call
def get_shareable_link(
    file_id: str = "",
    file_name: str = "",
    role: str = "reader"
) -> dict:
    """Get or create a shareable link for a file."""
    return share_file_publicly(file_id=file_id, file_name=file_name, role=role)


@log_tool_call
def remove_permission(
    file_id: str = "",
    file_name: str = "",
    permission_id: str = ""
) -> dict:
    """Remove a specific permission from a file."""
    if not permission_id:
        raise DriveValidationError("permission_id", permission_id, "required")
    
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    _api_call(lambda: drive.permissions().delete(
        fileId=resolved_id, permissionId=permission_id
    ).execute())
    
    return {
        "success": True,
        "file_id": resolved_id,
        "removed_permission": permission_id,
        "message": f"Permission {permission_id} removed.",
    }


@log_tool_call
def remove_access(
    file_id: str = "",
    file_name: str = "",
    email: str = ""
) -> dict:
    """Remove access for a specific email address."""
    if not email:
        raise DriveValidationError("email", email, "required")
    
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    perms = get_file_permissions(file_id=resolved_id)
    target = next((p for p in perms if p.get("email", "").lower() == email.lower()), None)
    
    if not target:
        return {
            "success": False,
            "error": f"No permission found for {email}.",
        }
    
    return remove_permission(file_id=resolved_id, permission_id=target["id"])


@log_tool_call
def make_file_private(
    file_id: str = "",
    file_name: str = ""
) -> dict:
    """Remove all public/anyone-with-link sharing from a file."""
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    perms = get_file_permissions(file_id=resolved_id)
    removed = 0
    errors = []
    
    for p in perms:
        if p.get("type") in ("anyone", "domain"):
            try:
                remove_permission(file_id=resolved_id, permission_id=p["id"])
                removed += 1
            except Exception as e:
                errors.append({"permission": p["id"], "error": str(e)})
    
    return {
        "success": True,
        "removed_count": removed,
        "errors": errors if errors else None,
        "file_id": resolved_id,
        "message": f"Removed {removed} public permission(s). File is now private." if removed else "File was already private.",
    }


# ═════════════════════════════════════════════════════════════════════════════
# UPLOAD
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def upload_file(
    file_path: str,
    folder_id: str = "",
    folder_name: str = "",
    file_name: str = ""
) -> dict:
    """
    Upload a local file to Google Drive.
    
    Args:
        file_path: Absolute path to local file
        folder_id: Destination folder ID
        folder_name: Destination folder name (alternative)
        file_name: Custom name for uploaded file
    
    Returns:
        Standardized file metadata
    """
    import mimetypes
    
    fpath = Path(file_path)
    if not fpath.exists():
        raise DriveNotFoundError(resource_id=file_path, resource_type="local file")
    
    name = file_name or fpath.name
    mime, _ = mimetypes.guess_type(str(fpath))
    mime = mime or "application/octet-stream"
    
    metadata: dict = {"name": name}
    
    # Resolve destination folder
    if folder_id:
        metadata["parents"] = [folder_id]
    elif folder_name:
        resolved_folder = _resolve_id(folder_name, "folder", allow_ambiguity=True)
        metadata["parents"] = [resolved_folder]
    
    drive = authenticate_drive()
    media = MediaFileUpload(str(fpath), mimetype=mime, resumable=False)
    
    uploaded = _api_call(lambda: drive.files().create(
        body=metadata,
        media_body=media,
        fields=_FILE_FIELDS,
    ).execute())
    
    invalidate_cache("list_files")
    
    return standardize_file_response(uploaded)


# ═════════════════════════════════════════════════════════════════════════════
# DOWNLOAD (OPTIONAL FEATURE)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def download_file(
    file_id: str = "",
    file_name: str = "",
    download_path: str = "",
    export_format: str = ""
) -> dict:
    """
    Download or export a file from Google Drive.
    
    Args:
        file_id: File ID to download
        file_name: File name (alternative, will be resolved)
        download_path: Local path to save file (default: auto-generate)
        export_format: For Google Workspace files (pdf, docx, xlsx, etc.)
    
    Returns:
        Download details
    """
    from app.core.config import DOWNLOADS_DIR
    
    # Resolve file
    resolved_id = file_id if file_id else _resolve_id(file_name, "file", allow_ambiguity=True)
    
    drive = authenticate_drive()
    
    # Get file metadata
    meta = _api_call(lambda: drive.files().get(
        fileId=resolved_id, fields="name,mimeType"
    ).execute())
    
    original_name = meta.get("name", "download")
    mime_type = meta.get("mimeType", "")
    
    # Determine download path
    if not download_path:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        download_path = str(DOWNLOADS_DIR / original_name)
    
    # Handle Google Workspace files (export)
    if mime_type.startswith("application/vnd.google-apps."):
        export_mimes = {
            "application/vnd.google-apps.document": "application/pdf",
            "application/vnd.google-apps.spreadsheet": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.google-apps.presentation": "application/pdf",
        }
        
        export_mime = export_format or export_mimes.get(mime_type, "application/pdf")
        
        request = drive.files().export_media(fileId=resolved_id, mimeType=export_mime)
        
        # Update filename with correct extension
        ext_map = {
            "application/pdf": ".pdf",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        }
        ext = ext_map.get(export_mime, ".export")
        download_path = download_path.rsplit(".", 1)[0] + ext
    else:
        # Binary download
        request = drive.files().get_media(fileId=resolved_id)
    
    # Execute download
    from googleapiclient.http import MediaIoBaseDownload
    import io
    
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    
    done = False
    while not done:
        status, done = downloader.next_chunk()
    
    # Save to file
    with open(download_path, "wb") as f:
        f.write(fh.getvalue())
    
    return {
        "success": True,
        "file_id": resolved_id,
        "original_name": original_name,
        "download_path": download_path,
        "mime_type": mime_type,
        "size_bytes": len(fh.getvalue()),
        "message": f"Downloaded to {download_path}",
    }


# ═════════════════════════════════════════════════════════════════════════════
# DUPLICATE DETECTION (OPTIONAL FEATURE)
# ═════════════════════════════════════════════════════════════════════════════

@log_tool_call
def find_duplicates(
    folder_id: str = "",
    folder_name: str = "",
    checksum: bool = False
) -> dict:
    """
    Find potential duplicate files.
    
    Args:
        folder_id: Folder to search in (default: entire Drive)
        folder_name: Folder name (alternative)
        checksum: Whether to compare by MD5 (slower but more accurate)
    
    Returns:
        Groups of potential duplicates
    """
    # Resolve folder
    search_folder = ""
    if folder_id:
        search_folder = folder_id
    elif folder_name:
        search_folder = _resolve_id(folder_name, "folder", allow_ambiguity=True)
    
    # Get all files
    all_files = []
    page_token = None
    drive = authenticate_drive()

    while True:
        params = {
            "q": "trashed=false" + (f" and '{search_folder}' in parents" if search_folder else ""),
            "pageSize": 100,
            "fields": "nextPageToken,files(id,name,size,md5Checksum,mimeType,modifiedTime)",
        }
        if page_token:
            params["pageToken"] = page_token

        results = _api_call(lambda: drive.files().list(**params).execute())
        
        all_files.extend(results.get("files", []))
        page_token = results.get("nextPageToken")
        
        if not page_token:
            break
    
    # Group by name (and optionally checksum)
    name_groups: dict[str, list] = {}
    for f in all_files:
        name = f.get("name", "")
        key = name
        if checksum and f.get("md5Checksum"):
            key = f"{name}:{f['md5Checksum']}"
        
        if key not in name_groups:
            name_groups[key] = []
        name_groups[key].append(f)
    
    # Filter to groups with >1 file
    duplicates = {
        k: [{"id": f["id"], "name": f["name"], "size": f.get("size", "")} for f in v]
        for k, v in name_groups.items()
        if len(v) > 1
    }
    
    return {
        "searched_count": len(all_files),
        "duplicate_groups": len(duplicates),
        "duplicates": duplicates,
        "checksum_used": checksum,
    }


# ═════════════════════════════════════════════════════════════════════════════
# OBSERVABILITY
# ═════════════════════════════════════════════════════════════════════════════

def get_drive_stats() -> dict:
    """Get statistics about Drive MCP usage."""
    return {
        "logging": _drive_logger.get_stats(),
        "cache": _drive_cache.stats(),
        "recent_calls": _drive_logger.get_recent_calls(5),
    }


def clear_drive_cache() -> dict:
    """Clear the Drive MCP cache."""
    _drive_cache.clear()
    return {"success": True, "message": "Cache cleared"}


# ═════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY EXPORTS
# ═════════════════════════════════════════════════════════════════════════════

# Ensure find_folder_by_name still works (for orchestrator compatibility)
find_folder_by_name = resolve_folder_id
