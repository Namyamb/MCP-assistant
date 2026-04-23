"""
Google Drive MCP Tools Registry v2

Provides 30+ tools for comprehensive Drive management.
All tools support name→ID resolution and agent-friendly responses.
"""

from app.integrations.drive import core_v2 as core

DRIVE_TOOLS: dict = {
    # ═══════════════════════════════════════════════════════════════════════
    # ID RESOLUTION (Agent-friendly naming)
    # ═══════════════════════════════════════════════════════════════════════
    "resolve_file_id":       core.resolve_file_id,
    "resolve_folder_id":     core.resolve_folder_id,

    # ═══════════════════════════════════════════════════════════════════════
    # BROWSE (with pagination)
    # ═══════════════════════════════════════════════════════════════════════
    "list_files":            core.list_files,
    "list_folders":          core.list_folders,
    "get_folder_contents":   core.get_folder_contents,
    "get_starred_files":     core.get_starred_files,
    "get_recent_files":      core.get_recent_files,

    # ═══════════════════════════════════════════════════════════════════════
    # ADVANCED SEARCH
    # ═══════════════════════════════════════════════════════════════════════
    "search_files":          core.search_files,
    "search_files_by_type":  core.search_files_by_type,

    # ═══════════════════════════════════════════════════════════════════════
    # METADATA & INTELLIGENCE
    # ═══════════════════════════════════════════════════════════════════════
    "get_file_metadata":     core.get_file_metadata,
    "get_storage_info":      core.get_storage_info,

    # ═══════════════════════════════════════════════════════════════════════
    # CREATE
    # ═══════════════════════════════════════════════════════════════════════
    "create_folder":         core.create_folder,

    # ═══════════════════════════════════════════════════════════════════════
    # ORGANIZE (name or ID accepted)
    # ═══════════════════════════════════════════════════════════════════════
    "rename_file":           core.rename_file,
    "move_file":             core.move_file,
    "copy_file":             core.copy_file,

    # ═══════════════════════════════════════════════════════════════════════
    # TRASH / DELETE / RESTORE
    # ═══════════════════════════════════════════════════════════════════════
    "trash_file":            core.trash_file,
    "restore_file":          core.restore_file,
    "delete_file":           core.delete_file,
    "trash_folder":          core.trash_folder,
    "restore_folder":        core.restore_folder,
    "delete_folder":         core.delete_folder,

    # ═══════════════════════════════════════════════════════════════════════
    # BATCH OPERATIONS (New)
    # ═══════════════════════════════════════════════════════════════════════
    "delete_files":          core.delete_files,
    "move_files":            core.move_files,
    "copy_files":            core.copy_files,

    # ═══════════════════════════════════════════════════════════════════════
    # SHARING & PERMISSIONS (with safety layer)
    # ═══════════════════════════════════════════════════════════════════════
    "get_file_permissions":  core.get_file_permissions,
    "share_file":            core.share_file,
    "share_file_publicly":   core.share_file_publicly,
    "get_shareable_link":    core.get_shareable_link,
    "remove_permission":     core.remove_permission,
    "remove_access":         core.remove_access,
    "make_file_private":     core.make_file_private,

    # ═══════════════════════════════════════════════════════════════════════
    # UPLOAD
    # ═══════════════════════════════════════════════════════════════════════
    "upload_file":           core.upload_file,

    # ═══════════════════════════════════════════════════════════════════════
    # DOWNLOAD (New)
    # ═══════════════════════════════════════════════════════════════════════
    "download_file":         core.download_file,

    # ═══════════════════════════════════════════════════════════════════════
    # UTILITY (New)
    # ═══════════════════════════════════════════════════════════════════════
    "find_duplicates":       core.find_duplicates,
    "get_drive_stats":       core.get_drive_stats,
    "clear_drive_cache":     core.clear_drive_cache,
}
