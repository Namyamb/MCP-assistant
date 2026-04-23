"""
Google Sheets MCP Tools Registry v2

Registers 30+ tools:
- v2 unified tools (recommended)
- v2 core tools (direct access)
- v1 backward-compatible wrappers
"""

from app.integrations.sheets import core_v2

SHEETS_TOOLS: dict = {
    # ═══════════════════════════════════════════════════════════════════════
    # V2 UNIFIED TOOLS (agent-facing, reduces fragmentation)
    # ═══════════════════════════════════════════════════════════════════════
    "resolve_sheet_id":         core_v2.resolve_sheet_id,
    "resolve_tab_name":         core_v2.resolve_tab_name,
    "resolve_range":            core_v2.resolve_range,
    "sheet_action":             core_v2.sheet_action,
    "sheet_modify":             core_v2.sheet_modify,
    "sheet_analyze":            core_v2.sheet_analyze,
    "sheet_structure":          core_v2.sheet_structure,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 METADATA & SEARCH
    # ═══════════════════════════════════════════════════════════════════════
    "get_sheet_metadata":       core_v2.get_sheet_metadata,
    "list_spreadsheets":        core_v2.list_spreadsheets,
    "search_spreadsheets":      core_v2.search_spreadsheets,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 DATA READ
    # ═══════════════════════════════════════════════════════════════════════
    "read_sheet_data":          core_v2.read_sheet_data,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 WRITE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "write_range":              core_v2.write_range,
    "append_rows":              core_v2.append_rows,
    "update_cell":              core_v2.update_cell,
    "clear_range":              core_v2.clear_range,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 BATCH OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "update_multiple_ranges":   core_v2.update_multiple_ranges,
    "append_rows_bulk":         core_v2.append_rows_bulk,
    "delete_rows_bulk":         core_v2.delete_rows_bulk,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 FORMULA SUPPORT
    # ═══════════════════════════════════════════════════════════════════════
    "insert_formula":           core_v2.insert_formula,
    "detect_formula_columns":   core_v2.detect_formula_columns,
    "compute_column_summary":   core_v2.compute_column_summary,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 DATA ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════
    "get_sheet_insights":       core_v2.get_sheet_insights,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 TAB MANAGEMENT
    # ═══════════════════════════════════════════════════════════════════════
    "add_tab":                  core_v2.add_tab,
    "rename_tab":               core_v2.rename_tab,
    "delete_tab":               core_v2.delete_tab,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 SPREADSHEET LIFECYCLE
    # ═══════════════════════════════════════════════════════════════════════
    "create_spreadsheet":       core_v2.create_spreadsheet,
    "delete_spreadsheet":       core_v2.delete_spreadsheet,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 CONTEXT & OBSERVABILITY
    # ═══════════════════════════════════════════════════════════════════════
    "get_sheets_context_summary": core_v2.get_sheets_context_summary,
    "get_sheets_stats":         core_v2.get_sheets_stats,
    "clear_sheets_cache":       core_v2.clear_sheets_cache,

    # ═══════════════════════════════════════════════════════════════════════
    # V1 BACKWARD-COMPATIBLE TOOLS (delegate to v2 internally)
    # ═══════════════════════════════════════════════════════════════════════
    "list_sheets":              core_v2.list_sheets,
    "search_sheets":            core_v2.search_sheets,
    "get_sheet":                core_v2.get_sheet,
    "read_sheet":               core_v2.read_sheet,
    "create_sheet":             core_v2.create_sheet,
    "write_to_sheet":           core_v2.write_to_sheet,
    "append_to_sheet":          core_v2.append_to_sheet,
    "clear_sheet_range":        core_v2.clear_sheet_range,
    "add_sheet_tab":            core_v2.add_sheet_tab,
    "rename_sheet_tab":         core_v2.rename_sheet_tab,
    "delete_sheet":             core_v2.delete_sheet,
}
