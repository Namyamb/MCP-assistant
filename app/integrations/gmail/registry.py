"""
Gmail MCP Tools Registry v2

Provides 50+ tools including:
- v1 compatible tools (core, ai)
- v2 unified tools (core_v2)
- ID resolution tools
- Batch operations
- Advanced search
"""

from app.integrations.gmail import core, ai, core_v2

GMAIL_TOOLS = {
    # ═══════════════════════════════════════════════════════════════════════
    # V2 UNIFIED TOOLS (Recommended - reduces fragmentation)
    # ═══════════════════════════════════════════════════════════════════════
    "resolve_email_id":      core_v2.resolve_email_id,
    "resolve_draft_id":      core_v2.resolve_draft_id,
    "email_action":          core_v2.email_action,
    "email_modify":          core_v2.email_modify,
    "email_generate":        core_v2.email_generate,
    "email_analyze":         core_v2.email_analyze,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 PAGINATED GETTERS
    # ═══════════════════════════════════════════════════════════════════════
    "get_emails_v2":         core_v2.get_emails,
    "search_emails_v2":      core_v2.search_emails,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 BATCH OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════
    "batch_email_action":    core_v2.batch_email_action,
    "archive_emails":        core_v2.archive_emails,
    "trash_emails":          core_v2.trash_emails,
    "delete_emails":         core_v2.delete_emails,
    "mark_emails_read":      core_v2.mark_emails_read,
    "star_emails":           core_v2.star_emails,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V2 UTILITY
    # ═══════════════════════════════════════════════════════════════════════
    "get_gmail_stats":       core_v2.get_gmail_stats,
    "clear_gmail_cache":     core_v2.clear_gmail_cache,
    
    # ═══════════════════════════════════════════════════════════════════════
    # V1 Core tools (backward compatible)
    "authenticate_gmail": core.authenticate_gmail,
    "get_emails": core.get_emails,
    "get_email_by_id": core.get_email_by_id,
    "get_unread_emails": core.get_unread_emails,
    "get_starred_emails": core.get_starred_emails,
    "search_emails": core.search_emails,
    "get_emails_by_sender": core.get_emails_by_sender,
    "get_emails_by_label": core.get_emails_by_label,
    "get_emails_by_date_range": core.get_emails_by_date_range,
    "get_email_thread": core.get_email_thread,
    "send_email": core.send_email,
    "send_email_with_attachment": core.send_email,
    "draft_email": core.draft_email,
    "send_draft": core.send_draft,
    "update_draft": core.update_draft,
    "delete_draft": core.delete_draft,
    "reply_email": core.reply_email,
    "reply_all": core.reply_all,
    "forward_email": core.forward_email,
    "list_labels": core.list_labels,
    "add_label": core.add_label,
    "remove_label": core.remove_label,
    "create_label": core.create_label,
    "delete_label": core.delete_label,
    "mark_as_read": core.mark_as_read,
    "mark_as_unread": core.mark_as_unread,
    "star_email": core.star_email,
    "unstar_email": core.unstar_email,
    "archive_email": core.archive_email,
    "unarchive_email": core.unarchive_email,
    "move_to_folder": core.move_to_folder,
    "trash_email": core.trash_email,
    "restore_email": core.restore_email,
    "delete_email": core.delete_email,
    "get_attachments": core.get_attachments,
    "download_attachment": core.download_attachment,
    "save_attachment_to_disk": core.save_attachment_to_disk,
    "schedule_email": core.schedule_email,
    "confirm_action": core.confirm_action,
    "validate_email_address": core.validate_email_address,
    "sanitize_email_content": core.sanitize_email_content,
    "log_email_action": core.log_email_action,
    "audit_email_history": core.audit_email_history,
    "count_emails_by_sender": core.count_emails_by_sender,
    "email_activity_summary": core.email_activity_summary,
    "most_frequent_contacts": core.most_frequent_contacts,
    
    # AI tools
    "summarize_email": ai.summarize_email,
    "summarize_emails": ai.summarize_emails,
    "classify_email": ai.classify_email,
    "detect_urgency": ai.detect_urgency,
    "detect_action_required": ai.detect_action_required,
    "sentiment_analysis": ai.sentiment_analysis,
    "extract_tasks": ai.extract_tasks,
    "extract_dates": ai.extract_dates,
    "extract_contacts": ai.extract_contacts,
    "extract_links": ai.extract_links,
    "draft_reply": ai.draft_reply,
    "generate_followup": ai.generate_followup,
    "auto_reply": ai.auto_reply,
    "rewrite_email": ai.rewrite_email,
    "translate_email": ai.translate_email,
    "auto_label_emails": ai.auto_label_emails,
    "auto_archive_promotions": ai.auto_archive_promotions,
    "auto_reply_rules": ai.auto_reply_rules,
}
