"""
Google Drive integration for G-Assistant - Backward Compatibility Layer

This module wraps core_v2 to maintain full backward compatibility.
All original function signatures are preserved while underlying implementation
uses the enhanced v2 features.

New code should use app.integrations.drive.core_v2 directly for full access
to new features like advanced search, batch operations, and structured responses.

Auth note: requires ALL_SCOPES (drive scope is included).
If authentication fails, delete credentials/token.json and run `python auth.py`.
"""

from __future__ import annotations

# ═════════════════════════════════════════════════════════════════════════════
# RE-EXPORT FROM CORE_V2 (with backward compatibility wrappers)
# ═════════════════════════════════════════════════════════════════════════════

# Import core infrastructure
from app.integrations.drive.core_v2 import (
    # Auth (internal use)
    authenticate_drive,
    # Utilities
    validate_email,
    standardize_file_response,
    get_mcp_suggestion,
    paginated_response,
)

# Import error classes from utils (where they're defined)
from app.integrations.drive.utils import (
    DriveError,
    DriveNotFoundError,
    DrivePermissionError,
    DriveRateLimitError,
    DriveValidationError,
    DriveAmbiguityError,
    # Cache
    _drive_cache,
    invalidate_cache,
    # Logger
    _drive_logger,
)

# Keep thread-local for any direct auth usage
import threading
_thread_local = threading.local()



# ═════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY WRAPPERS
# These wrap v2 functions to return backward-compatible list formats
# ═════════════════════════════════════════════════════════════════════════════

# Import v2 functions for wrapping
from app.integrations.drive import core_v2


def _extract_list_from_response(response: dict) -> list:
    """Extract file list from v2 paginated response for backward compatibility."""
    if isinstance(response, dict) and "files" in response:
        return response["files"]
    return response if isinstance(response, list) else []


def list_files(limit: int = 10, folder_id: str = "") -> list[dict]:
    """
    List files in Drive (or inside a specific folder), most recently modified first.
    
    Backward-compatible: returns list of files (not paginated response).
    For pagination support, use core_v2.list_files directly.
    """
    result = core_v2.list_files(limit=limit, folder_id=folder_id)
    return _extract_list_from_response(result)


def list_folders(limit: int = 20) -> list[dict]:
    """
    List all folders in Drive, most recently modified first.
    
    Backward-compatible: returns list of folders.
    """
    result = core_v2.list_folders(limit=limit)
    return _extract_list_from_response(result)


def find_folder_by_name(name: str) -> Optional[dict]:
    """
    Return the first folder whose name exactly matches (case-insensitive), or None.
    
    Backward-compatible: returns single folder dict or None.
    For better error handling, use core_v2.resolve_folder_id.
    """
    try:
        folder_id = core_v2.resolve_folder_id(name, allow_ambiguity=True)
        return core_v2.get_file_metadata(folder_id)
    except DriveNotFoundError:
        return None


def get_folder_contents(folder_id: str, limit: int = 20) -> list[dict]:
    """List all files and subfolders inside a specific folder."""
    result = core_v2.get_folder_contents(folder_id=folder_id, limit=limit)
    return _extract_list_from_response(result)


def get_starred_files(limit: int = 20) -> list[dict]:
    """List starred (favourited) files."""
    result = core_v2.get_starred_files(limit=limit)
    return _extract_list_from_response(result)


def get_recent_files(limit: int = 10) -> list[dict]:
    """List the most recently modified files in Drive."""
    result = core_v2.get_recent_files(limit=limit)
    return _extract_list_from_response(result)


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

def search_files(query: str, limit: int = 10) -> list[dict]:
    """
    Search files by name or full-text content.
    
    Backward-compatible: returns list of files.
    For advanced search with filters, use core_v2.search_files.
    """
    result = core_v2.search_files(query=query, limit=limit)
    return _extract_list_from_response(result)


def search_files_by_type(file_type: str, limit: int = 10) -> list[dict]:
    """
    Search files by type keyword.
    file_type examples: 'doc', 'sheet', 'pdf', 'image', 'folder', 'slides'
    
    Backward-compatible: returns list of files.
    """
    result = core_v2.search_files_by_type(file_type=file_type, limit=limit)
    return _extract_list_from_response(result)


# ─────────────────────────────────────────────────────────────────────────────
# Metadata
# ─────────────────────────────────────────────────────────────────────────────

def get_file_metadata(file_id: str) -> dict:
    """
    Get full metadata for a file or folder.
    
    Backward-compatible: works with file_id only.
    For name resolution, use core_v2.get_file_metadata.
    """
    return core_v2.get_file_metadata(file_id=file_id)


def get_storage_info() -> dict:
    """Return Drive storage quota: used, total, and free space (in bytes)."""
    return core_v2.get_storage_info()


# ─────────────────────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────────────────────

def create_folder(name: str, parent_id: str = "") -> dict:
    """
    Create a new folder in Drive (optionally inside another folder).
    
    Backward-compatible: returns dict with id, name, type, url, message.
    For parent by name, use core_v2.create_folder with parent_name.
    """
    result = core_v2.create_folder(name=name, parent_id=parent_id)
    # Maintain original response format
    return {
        "id":      result.get("id", ""),
        "name":    result.get("name", name),
        "type":    "Folder",
        "url":     result.get("url", ""),
        "message": f"Folder '{name}' created successfully.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Organise
# ─────────────────────────────────────────────────────────────────────────────

def rename_file(file_id: str, new_name: str) -> dict:
    """
    Rename a file or folder.
    
    Backward-compatible: returns dict with success, id, name, message.
    For name resolution, use core_v2.rename_file.
    """
    result = core_v2.rename_file(file_id=file_id, new_name=new_name)
    return {
        "success": True,
        "id":      result.get("id", file_id),
        "name":    new_name,
        "message": f"Renamed to '{new_name}'.",
    }


def move_file(file_id: str, destination_folder_id: str) -> dict:
    """
    Move a file into a different folder.
    
    Backward-compatible: returns dict with success, id, parents, message.
    For name resolution on either parameter, use core_v2.move_file.
    """
    result = core_v2.move_file(file_id=file_id, destination_folder_id=destination_folder_id)
    return {
        "success": True,
        "id":      file_id,
        "parents": result.get("parent", destination_folder_id),
        "message": f"File moved to folder '{destination_folder_id}'.",
    }


def copy_file(file_id: str, new_name: str = "", destination_folder_id: str = "") -> dict:
    """
    Copy a file, optionally with a new name and/or into a different folder.
    
    Backward-compatible: returns dict with success, id, name, url, message.
    """
    result = core_v2.copy_file(
        file_id=file_id,
        new_name=new_name,
        destination_folder_id=destination_folder_id
    )
    return {
        "success": True,
        "id":      result.get("id", ""),
        "name":    result.get("name", new_name or "Copy"),
        "url":     result.get("url", ""),
        "message": f"File copied as '{result.get('name', 'Copy')}'.",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Trash / Delete / Restore
# ─────────────────────────────────────────────────────────────────────────────

def trash_file(file_id: str) -> dict:
    """
    Move a file or folder to the Drive trash.
    
    Backward-compatible: returns dict with success and message.
    """
    core_v2.trash_file(file_id=file_id)
    return {"success": True, "message": f"File {file_id} moved to trash."}


def restore_file(file_id: str) -> dict:
    """
    Restore a file or folder from the Drive trash.
    
    Backward-compatible: returns dict with success and message.
    """
    core_v2.restore_file(file_id=file_id)
    return {"success": True, "message": f"File {file_id} restored from trash."}


def delete_file(file_id: str) -> dict:
    """
    Permanently delete a file or folder (cannot be undone).
    
    Backward-compatible: returns dict with success and message.
    """
    core_v2.delete_file(file_id=file_id)
    return {"success": True, "message": f"File {file_id} permanently deleted."}


def trash_folder(folder_id: str = "", folder_name: str = "") -> dict:
    """Move a folder to trash (alias for trash_file)."""
    core_v2.trash_folder(folder_id=folder_id, folder_name=folder_name)
    return {"success": True, "message": f"Folder {folder_id or folder_name} moved to trash."}


def delete_folder(folder_id: str = "", folder_name: str = "") -> dict:
    """Permanently delete a folder (alias for delete_file)."""
    core_v2.delete_folder(folder_id=folder_id, folder_name=folder_name)
    return {"success": True, "message": f"Folder {folder_id or folder_name} permanently deleted."}


def restore_folder(folder_id: str = "", folder_name: str = "") -> dict:
    """Restore a folder from trash (alias for restore_file)."""
    core_v2.restore_folder(folder_id=folder_id, folder_name=folder_name)
    return {"success": True, "message": f"Folder {folder_id or folder_name} restored from trash."}


# ─────────────────────────────────────────────────────────────────────────────
# Sharing & Permissions
# ─────────────────────────────────────────────────────────────────────────────

def get_file_permissions(file_id: str) -> list[dict]:
    """List all sharing permissions for a file or folder."""
    return core_v2.get_file_permissions(file_id=file_id)


def share_file(file_id: str, email: str, role: str = "reader") -> dict:
    """
    Share a file with a specific user.
    role: 'reader' | 'commenter' | 'writer'
    
    Note: 'owner' role is blocked for safety (use Drive web UI for ownership transfer).
    """
    return core_v2.share_file(file_id=file_id, email=email, role=role)


def share_file_publicly(file_id: str, role: str = "reader") -> dict:
    """Make a file accessible to anyone with the link."""
    return core_v2.share_file_publicly(file_id=file_id, role=role)


def get_shareable_link(file_id: str) -> dict:
    """Get the shareable web link for a file (makes it public if not already)."""
    return core_v2.get_shareable_link(file_id=file_id)


def remove_permission(file_id: str, permission_id: str) -> dict:
    """Remove a specific sharing permission from a file."""
    return core_v2.remove_permission(file_id=file_id, permission_id=permission_id)


def remove_access(file_id: str, email: str) -> dict:
    """Remove access for a specific email address (finds and deletes their permission)."""
    return core_v2.remove_access(file_id=file_id, email=email)


def make_file_private(file_id: str) -> dict:
    """Remove all public / anyone-with-link sharing from a file."""
    result = core_v2.make_file_private(file_id=file_id)
    # Maintain backward-compatible message format
    if result.get("removed_count", 0) > 0:
        return {
            "success": True,
            "message": f"Removed {result['removed_count']} public permission(s). File is now private."
        }
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
    return core_v2.upload_file(
        file_path=file_path,
        folder_id=folder_id or "",
        file_name=file_name or ""
    )
