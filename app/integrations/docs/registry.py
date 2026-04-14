from app.integrations.docs import core

DOCS_TOOLS: dict = {
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
