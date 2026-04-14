from app.integrations.sheets import core

SHEETS_TOOLS: dict = {
    "list_sheets":       core.list_sheets,
    "search_sheets":     core.search_sheets,
    "get_sheet":         core.get_sheet,
    "read_sheet":        core.read_sheet,
    "create_sheet":      core.create_sheet,
    "write_to_sheet":    core.write_to_sheet,
    "append_to_sheet":   core.append_to_sheet,
    "clear_sheet_range": core.clear_sheet_range,
    "add_sheet_tab":     core.add_sheet_tab,
    "rename_sheet_tab":  core.rename_sheet_tab,
    "delete_sheet":      core.delete_sheet,
}
