from app.integrations.drive import core

DRIVE_TOOLS: dict = {
    # Browse
    "list_files":            core.list_files,
    "list_folders":          core.list_folders,
    "get_folder_contents":   core.get_folder_contents,
    "get_starred_files":     core.get_starred_files,
    "get_recent_files":      core.get_recent_files,
    # Search
    "search_files":          core.search_files,
    "search_files_by_type":  core.search_files_by_type,
    # Metadata
    "get_file_metadata":     core.get_file_metadata,
    "get_storage_info":      core.get_storage_info,
    # Create
    "create_folder":         core.create_folder,
    # Organise
    "rename_file":           core.rename_file,
    "move_file":             core.move_file,
    "copy_file":             core.copy_file,
    # Trash / Delete / Restore
    "trash_file":            core.trash_file,
    "restore_file":          core.restore_file,
    "delete_file":           core.delete_file,
    # Sharing & Permissions
    "get_file_permissions":  core.get_file_permissions,
    "share_file":            core.share_file,
    "share_file_publicly":   core.share_file_publicly,
    "get_shareable_link":    core.get_shareable_link,
    "remove_permission":     core.remove_permission,
    "remove_access":         core.remove_access,
    "make_file_private":     core.make_file_private,
    # Upload
    "upload_file":           core.upload_file,
}
