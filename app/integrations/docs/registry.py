"""
Google Docs MCP Tools Registry v2

Provides 30+ tools including:
- v1 compatible tools (core)
- v2 unified tools (core_v2)
- ID resolution tools
- Content analysis tools
"""

from app.integrations.docs import core, core_v2

DOCS_TOOLS: dict = {
    # ═══════════════════════════════════════════════════════════════════════
    # V2 UNIFIED TOOLS (Recommended - reduces fragmentation)
    # ═══════════════════════════════════════════════════════════════════════
    "resolve_doc_id":      core_v2.resolve_doc_id,
    "doc_action":          core_v2.doc_action,
    "doc_modify":          core_v2.doc_modify,
    "doc_create":          core_v2.doc_create,
    "doc_analyze":         core_v2.doc_analyze,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 PAGINATED READ
    # ═══════════════════════════════════════════════════════════════════════
    "read_document":       core_v2.read_document,
    "list_documents":      core_v2.list_documents,
    "search_documents":    core_v2.search_documents,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 CONTENT OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "append_content":      core_v2.append_content,
    "insert_content":      core_v2.insert_content,
    "replace_section":     core_v2.replace_section,
    "delete_section":      core_v2.delete_section,
    "clear_document":      core_v2.clear_document,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 METADATA & CREATION
    # ═══════════════════════════════════════════════════════════════════════
    "get_document_metadata": core_v2.get_document_metadata,
    "create_blank_document": core_v2.create_blank_document,
    "create_from_template": core_v2.create_from_template,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════
    "analyze_summary":     core_v2.analyze_summary,
    "analyze_structure":   core_v2.analyze_structure,
    "analyze_key_points":  core_v2.analyze_key_points,
    "analyze_action_items": core_v2.analyze_action_items,
    "analyze_word_count":  core_v2.analyze_word_count,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 BATCH SECTION OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "append_multiple_sections":  core_v2.append_multiple_sections,
    "replace_multiple_sections": core_v2.replace_multiple_sections,
    "delete_multiple_sections":  core_v2.delete_multiple_sections,

    # ═══════════════════════════════════════════════════════════════════════
    # V2 UTILITY
    # ═══════════════════════════════════════════════════════════════════════
    "get_docs_context_summary": core_v2.get_docs_context_summary,
    "get_docs_stats":      core_v2.get_docs_stats,
    "clear_docs_cache":    core_v2.clear_docs_cache,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V1 Core Tools (Backward Compatible)
    # ═══════════════════════════════════════════════════════════════════════
    "list_docs":           core.list_docs,
    "search_docs":         core.search_docs,
    "get_doc":             core.get_doc,
    "get_doc_content":     core.get_doc_content,
    "create_doc":          core.create_doc,
    "append_to_doc":       core.append_to_doc,
    "replace_text_in_doc": core.replace_text_in_doc,
    "update_doc_title":    core.update_doc_title,
    "delete_doc":          core.delete_doc,
}
