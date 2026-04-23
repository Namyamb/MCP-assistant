"""
orchestrator_v2.py — Context-Aware Agent Orchestration for G-Assistant

Architecture:
  1. State Management: Structured context with entities and recent actions
  2. Intent Detection: STRICT rules - no pronouns, explicit IDs only
  3. LLM Resolution: Pronouns handled via conversation history + context
  4. Safety Layer: Context injection for missing arguments
  5. History: Tool summaries with clear entity IDs, per-mode

Supports: Gmail, Docs, Sheets, Drive
"""

from __future__ import annotations

import json
import logging
import re
import datetime
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field

from app.core.llm_client import call_model
from app.integrations.gmail.registry import GMAIL_TOOLS
from app.integrations.docs.registry import DOCS_TOOLS
from app.integrations.sheets.registry import SHEETS_TOOLS
from app.integrations.drive.registry import DRIVE_TOOLS
from app.integrations.drive.core import find_folder_by_name

logger = logging.getLogger(__name__)


# ============================================================================
# SECTION 1: STATE STRUCTURE
# ============================================================================

@dataclass
class Entity:
    """Represents a single tracked entity (email, doc, file, etc.)."""
    id: str
    type: str  # "email", "draft", "thread", "doc", "sheet", "file", "folder"
    attributes: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "attributes": self.attributes,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class ContextState:
    """Structured context for conversation state across all modes."""
    # Current primary entity per type
    entities: Dict[str, Optional[Entity]] = field(default_factory=lambda: {
        # Gmail
        "email": None,
        "draft": None,
        "thread": None,
        "attachment": None,
        "label": None,
        # Docs
        "doc": None,
        # Sheets
        "sheet": None,
        # Drive
        "file": None,
        "folder": None,
    })

    # Recency stack — most recent first, max 10
    recent_entities: List[Entity] = field(default_factory=list)

    last_action: str = ""
    last_action_timestamp: Optional[datetime.datetime] = None

    # Pending multi-turn action
    pending_action: Optional[Dict[str, Any]] = None

    # Per-mode conversation history: {"history_gmail": [...], "history_docs": [...], ...}
    history: Dict[str, List[Dict]] = field(default_factory=dict)

    active_mode: str = "gmail"

    # Gmail convenience state (persists across turns)
    last_draft_id: str = ""
    last_to: str = ""
    last_subject: str = ""
    last_body: str = ""
    last_viewed_ids: List[str] = field(default_factory=list)  # ordered by recency
    last_email_list: List[Dict] = field(default_factory=list)  # [{id, subject, from}, ...] from last email list
    pending_attachment_path: str = ""  # cleared after use

    def get_entity(self, entity_type: str) -> Optional[Entity]:
        return self.entities.get(entity_type)

    def set_entity(self, entity_type: str, entity: Entity):
        self.entities[entity_type] = entity
        self._add_to_recent(entity)

    def _add_to_recent(self, entity: Entity):
        self.recent_entities = [e for e in self.recent_entities if e.id != entity.id]
        self.recent_entities.insert(0, entity)
        self.recent_entities = self.recent_entities[:10]

    def get_by_pronoun(self, pronoun: str) -> Optional[Entity]:
        pronoun = pronoun.lower()
        if not self.recent_entities:
            return None
        if pronoun in ("this", "that", "it", "the one"):
            return self.recent_entities[0]
        if pronoun == "that one" and len(self.recent_entities) > 1:
            return self.recent_entities[1]
        return None

    def to_dict(self) -> dict:
        return {
            "entities": {k: (v.to_dict() if v else None) for k, v in self.entities.items()},
            "recent_entities": [e.to_dict() for e in self.recent_entities],
            "last_action": self.last_action,
            "last_action_timestamp": (
                self.last_action_timestamp.isoformat() if self.last_action_timestamp else None
            ),
            "pending_action": self.pending_action,
            "active_mode": self.active_mode,
        }


def get_or_create_context(state: dict) -> ContextState:
    if "_context_v2" not in state:
        state["_context_v2"] = ContextState()
    return state["_context_v2"]


def reset_mode_context(state: dict, mode: str):
    """Clear history and entities for a specific mode."""
    ctx = get_or_create_context(state)
    ctx.history.pop(f"history_{mode}", None)
    mode_entities = {
        "gmail":  ("email", "draft", "thread", "attachment", "label"),
        "docs":   ("doc",),
        "sheets": ("sheet",),
        "drive":  ("file", "folder"),
    }
    for entity_type in mode_entities.get(mode, ()):
        ctx.entities[entity_type] = None
    ctx.recent_entities = [
        e for e in ctx.recent_entities
        if e.type not in mode_entities.get(mode, ())
    ]
    if mode == "gmail":
        ctx.last_draft_id = ""
        ctx.last_to = ""
        ctx.last_subject = ""
        ctx.last_body = ""
        ctx.last_viewed_ids = []
        ctx.last_email_list = []
        ctx.pending_attachment_path = ""


# ============================================================================
# SECTION 2: CONTEXT UPDATE FUNCTIONS
# ============================================================================

def update_context_from_tool_result(
    ctx: ContextState,
    tool_name: str,
    result: dict,
    args: dict = None
):
    """Update context state after a successful tool execution."""
    if not result.get("success"):
        ctx.last_action = f"{tool_name}_failed"
        ctx.last_action_timestamp = datetime.datetime.now()
        return

    res_data = result.get("result", {})

    # ── Gmail ─────────────────────────────────────────────────────────────────
    if tool_name in ("get_emails", "get_unread_emails", "search_emails",
                     "get_starred_emails", "get_emails_by_sender",
                     "get_emails_by_label", "get_emails_by_date_range",
                     "get_emails_v2", "search_emails_v2"):
        # Handle multiple response formats
        emails = []
        if isinstance(res_data, dict):
            # v1.5 format (emails key)
            if "emails" in res_data and res_data.get("success", True):
                emails = res_data.get("emails", [])
            # v2 format (items key)
            elif "items" in res_data:
                emails = res_data.get("items", [])
        elif isinstance(res_data, list):
            # Legacy v1 format
            emails = res_data
        
        if emails:
            first = emails[0]
            ctx.set_entity("email", Entity(
                id=first.get("id", ""),
                type="email",
                attributes={
                    "subject": first.get("subject", ""),
                    "from": first.get("from", ""),
                    "snippet": first.get("snippet", "")[:100],
                    "total_found": len(emails),
                }
            ))
            ctx.last_viewed_ids = [e.get("id", "") for e in emails if e.get("id")]
            ctx.last_email_list = [
                {"id": e.get("id", ""), "subject": e.get("subject", e.get("snippet", "")[:60]), "from": e.get("from", "unknown")}
                for e in emails[:5] if e.get("id")
            ]
        ctx.last_action = f"{tool_name}_retrieved"

    elif tool_name == "get_email_by_id":
        eid = res_data.get("id", "") if isinstance(res_data, dict) else ""
        ctx.set_entity("email", Entity(
            id=eid,
            type="email",
            attributes={
                "subject": res_data.get("subject", ""),
                "from": res_data.get("from", ""),
                "to": res_data.get("to", ""),
                "body_snippet": res_data.get("body", "")[:200],
            }
        ))
        if eid:
            if eid in ctx.last_viewed_ids:
                ctx.last_viewed_ids.remove(eid)
            ctx.last_viewed_ids.insert(0, eid)
            ctx.last_viewed_ids = ctx.last_viewed_ids[:20]
        ctx.last_action = "email_viewed"

    elif tool_name == "send_email":
        mid = res_data.get("id", "") if isinstance(res_data, dict) else ""
        if mid:
            ctx.set_entity("email", Entity(id=mid, type="email",
                attributes={"sent": True, "to": (args or {}).get("to", "")}))
        ctx.last_to = (args or {}).get("to", "")
        ctx.last_subject = (args or {}).get("subject", "")
        ctx.last_body = (args or {}).get("body", "")
        ctx.pending_attachment_path = ""
        ctx.last_action = "email_sent"

    elif tool_name == "draft_email":
        draft_id = res_data.get("id", "") if isinstance(res_data, dict) else ""
        if draft_id:
            ctx.set_entity("draft", Entity(
                id=draft_id,
                type="draft",
                attributes={
                    "to": args.get("to", "") if args else "",
                    "subject": args.get("subject", "") if args else "",
                }
            ))
            ctx.last_draft_id = draft_id
        ctx.last_to = (args or {}).get("to", "")
        ctx.last_subject = (args or {}).get("subject", "")
        ctx.last_body = (args or {}).get("body", "")
        ctx.pending_attachment_path = ""
        ctx.last_action = "draft_created"

    elif tool_name == "update_draft":
        draft_id = (args or {}).get("draft_id", "")
        if draft_id:
            ctx.set_entity("draft", Entity(
                id=draft_id,
                type="draft",
                attributes={"updated": True, "subject": (args or {}).get("subject", "")}
            ))
        ctx.last_action = "draft_updated"

    elif tool_name == "send_draft":
        ctx.entities["draft"] = None
        ctx.last_draft_id = ""
        ctx.last_action = "draft_sent"

    elif tool_name == "delete_draft":
        ctx.entities["draft"] = None
        ctx.last_action = "draft_deleted"

    elif tool_name == "trash_email":
        trashed_id = (args or {}).get("message_id", "")
        if ctx.entities.get("email") and ctx.entities["email"].id == trashed_id:
            ctx.entities["email"] = None
        ctx.last_action = "email_trashed"

    elif tool_name == "get_email_thread":
        ctx.set_entity("thread", Entity(
            id=res_data.get("thread_id", "") if isinstance(res_data, dict) else "",
            type="thread",
            attributes={
                "message_count": len(res_data.get("messages", [])) if isinstance(res_data, dict) else 0,
                "subject": res_data.get("subject", "") if isinstance(res_data, dict) else "",
            }
        ))
        ctx.last_action = "thread_viewed"

    elif tool_name in ("add_label", "remove_label"):
        ctx.last_action = f"label_{tool_name}"

    elif tool_name == "list_labels":
        ctx.last_action = "labels_listed"

    # ── Docs ──────────────────────────────────────────────────────────────────
    elif tool_name in ("list_docs", "search_docs"):
        docs = res_data if isinstance(res_data, list) else []
        if docs:
            first = docs[0]
            ctx.set_entity("doc", Entity(
                id=first.get("id", ""),
                type="doc",
                attributes={
                    "title": first.get("title", ""),
                    "url": first.get("url", ""),
                    "total_found": len(docs),
                }
            ))
        ctx.last_action = f"{tool_name}_retrieved"

    elif tool_name in ("get_doc", "get_doc_content"):
        if isinstance(res_data, dict):
            ctx.set_entity("doc", Entity(
                id=res_data.get("id", ""),
                type="doc",
                attributes={
                    "title": res_data.get("title", ""),
                    "url": res_data.get("url", ""),
                }
            ))
        ctx.last_action = "doc_viewed"

    elif tool_name == "create_doc":
        if isinstance(res_data, dict):
            ctx.set_entity("doc", Entity(
                id=res_data.get("id", ""),
                type="doc",
                attributes={
                    "title": res_data.get("title", ""),
                    "url": res_data.get("url", ""),
                }
            ))
        ctx.last_action = "doc_created"

    elif tool_name in ("append_to_doc", "replace_text_in_doc", "update_doc_title"):
        ctx.last_action = f"{tool_name}_done"

    elif tool_name == "delete_doc":
        ctx.entities["doc"] = None
        ctx.last_action = "doc_deleted"

    # ── Sheets ────────────────────────────────────────────────────────────────
    elif tool_name in ("list_sheets", "search_sheets"):
        sheets = res_data if isinstance(res_data, list) else []
        if sheets:
            first = sheets[0]
            ctx.set_entity("sheet", Entity(
                id=first.get("id", ""),
                type="sheet",
                attributes={
                    "title": first.get("title", ""),
                    "url": first.get("url", ""),
                    "total_found": len(sheets),
                }
            ))
        ctx.last_action = f"{tool_name}_retrieved"

    elif tool_name in ("get_sheet", "read_sheet"):
        if isinstance(res_data, dict):
            ctx.set_entity("sheet", Entity(
                id=res_data.get("id", ""),
                type="sheet",
                attributes={
                    "title": res_data.get("title", ""),
                    "url": res_data.get("url", ""),
                }
            ))
        ctx.last_action = "sheet_viewed"

    elif tool_name == "create_sheet":
        if isinstance(res_data, dict):
            ctx.set_entity("sheet", Entity(
                id=res_data.get("id", ""),
                type="sheet",
                attributes={
                    "title": res_data.get("title", ""),
                    "url": res_data.get("url", ""),
                }
            ))
        ctx.last_action = "sheet_created"

    elif tool_name in ("write_to_sheet", "append_to_sheet", "clear_sheet_range",
                       "add_sheet_tab", "rename_sheet_tab"):
        ctx.last_action = f"{tool_name}_done"

    elif tool_name == "delete_sheet":
        ctx.entities["sheet"] = None
        ctx.last_action = "sheet_deleted"

    # ── Drive ─────────────────────────────────────────────────────────────────
    elif tool_name in ("list_files", "search_files", "search_files_by_type",
                       "get_starred_files", "get_recent_files", "get_folder_contents"):
        files = res_data if isinstance(res_data, list) else []
        if files:
            first = files[0]
            ctx.set_entity("file", Entity(
                id=first.get("id", ""),
                type="file",
                attributes={
                    "name": first.get("name", ""),
                    "file_type": first.get("type", ""),
                    "url": first.get("url", ""),
                    "total_found": len(files),
                }
            ))
        ctx.last_action = f"{tool_name}_retrieved"

    elif tool_name in ("list_folders",):
        folders = res_data if isinstance(res_data, list) else []
        if folders:
            first = folders[0]
            ctx.set_entity("folder", Entity(
                id=first.get("id", ""),
                type="folder",
                attributes={
                    "name": first.get("name", ""),
                    "url": first.get("url", ""),
                    "total_found": len(folders),
                }
            ))
        ctx.last_action = "list_folders_retrieved"

    elif tool_name == "get_file_metadata":
        if isinstance(res_data, dict):
            ctx.set_entity("file", Entity(
                id=res_data.get("id", ""),
                type="file",
                attributes={
                    "name": res_data.get("name", ""),
                    "file_type": res_data.get("type", ""),
                    "url": res_data.get("url", ""),
                }
            ))
        ctx.last_action = "file_viewed"

    elif tool_name == "create_folder":
        if isinstance(res_data, dict):
            ctx.set_entity("folder", Entity(
                id=res_data.get("id", ""),
                type="folder",
                attributes={"name": res_data.get("name", "")}
            ))
        ctx.last_action = "folder_created"

    elif tool_name == "upload_file":
        if isinstance(res_data, dict):
            ctx.set_entity("file", Entity(
                id=res_data.get("id", ""),
                type="file",
                attributes={"name": res_data.get("name", "")}
            ))
        ctx.last_action = "file_uploaded"

    elif tool_name in ("rename_file", "move_file", "copy_file", "share_file",
                       "share_file_publicly", "remove_permission", "remove_access",
                       "make_file_private"):
        ctx.last_action = f"{tool_name}_done"

    elif tool_name in ("trash_file", "delete_file"):
        trashed_id = (args or {}).get("file_id", "")
        if ctx.entities.get("file") and ctx.entities["file"].id == trashed_id:
            ctx.entities["file"] = None
        ctx.last_action = f"{tool_name}_done"

    elif tool_name in ("trash_folder", "delete_folder"):
        trashed_id = (args or {}).get("folder_id", "")
        if ctx.entities.get("folder") and ctx.entities["folder"].id == trashed_id:
            ctx.entities["folder"] = None
        ctx.last_action = f"{tool_name}_done"

    elif tool_name in ("restore_file", "restore_folder"):
        ctx.last_action = f"{tool_name}_done"

    ctx.last_action_timestamp = datetime.datetime.now()


# ============================================================================
# SECTION 3: TOOL EXECUTION SAFETY LAYER
# ============================================================================

def resolve_arguments(
    tool_name: str,
    args: dict,
    ctx: ContextState
) -> tuple[dict, list]:
    """
    Resolve missing required arguments from context.
    Returns: (resolved_args, missing_fields_list)
    """
    resolved = dict(args)
    missing = []

    # ── Gmail: message_id ─────────────────────────────────────────────────────
    if tool_name in ("trash_email", "archive_email", "star_email", "unstar_email",
                     "reply_email", "reply_all", "forward_email", "get_email_by_id",
                     "download_attachment", "save_attachment_to_disk",
                     "mark_as_read", "mark_as_unread", "restore_email", "delete_email",
                     "get_attachments", "summarize_email", "classify_email",
                     "detect_urgency", "detect_action_required", "sentiment_analysis",
                     "extract_tasks", "extract_dates", "extract_contacts",
                     "extract_links", "draft_reply", "generate_followup",
                     "auto_reply", "rewrite_email", "translate_email"):
        if not resolved.get("message_id"):
            entity = ctx.get_entity("email")
            if entity:
                resolved["message_id"] = entity.id
            else:
                missing.append("message_id")

    # ── Gmail: draft_id ───────────────────────────────────────────────────────
    elif tool_name in ("delete_draft", "send_draft", "update_draft"):
        if not resolved.get("draft_id"):
            entity = ctx.get_entity("draft")
            if entity:
                resolved["draft_id"] = entity.id
            else:
                missing.append("draft_id")

    # ── Gmail: label operations ───────────────────────────────────────────────
    elif tool_name in ("add_label", "remove_label"):
        if not resolved.get("message_id"):
            entity = ctx.get_entity("email")
            if entity:
                resolved["message_id"] = entity.id
            else:
                missing.append("message_id")

    # ── Gmail: thread ─────────────────────────────────────────────────────────
    elif tool_name == "get_email_thread":
        if not resolved.get("thread_id"):
            thread = ctx.get_entity("thread")
            if thread:
                resolved["thread_id"] = thread.id
            else:
                email = ctx.get_entity("email")
                if email and email.attributes.get("thread_id"):
                    resolved["thread_id"] = email.attributes["thread_id"]
                else:
                    missing.append("thread_id")

    # ── Docs: doc_id ──────────────────────────────────────────────────────────
    elif tool_name in ("get_doc", "get_doc_content", "append_to_doc",
                       "replace_text_in_doc", "update_doc_title", "delete_doc"):
        if not resolved.get("doc_id"):
            entity = ctx.get_entity("doc")
            if entity:
                resolved["doc_id"] = entity.id
            else:
                missing.append("doc_id")

    # ── Sheets: sheet_id ──────────────────────────────────────────────────────
    elif tool_name in ("get_sheet", "read_sheet", "write_to_sheet", "append_to_sheet",
                       "clear_sheet_range", "add_sheet_tab", "rename_sheet_tab",
                       "delete_sheet"):
        if not resolved.get("sheet_id"):
            entity = ctx.get_entity("sheet")
            if entity:
                resolved["sheet_id"] = entity.id
            else:
                missing.append("sheet_id")

    # ── Drive: file_id ────────────────────────────────────────────────────────
    elif tool_name in ("get_file_metadata", "rename_file", "move_file", "copy_file",
                       "trash_file", "restore_file", "delete_file",
                       "get_file_permissions", "share_file", "share_file_publicly",
                       "get_shareable_link", "remove_permission", "remove_access",
                       "make_file_private"):
        if not resolved.get("file_id"):
            entity = ctx.get_entity("file")
            if entity:
                resolved["file_id"] = entity.id
            else:
                missing.append("file_id")

    # ── Drive: folder_id ──────────────────────────────────────────────────────
    elif tool_name in ("get_folder_contents", "trash_folder",
                       "restore_folder", "delete_folder"):
        if not resolved.get("folder_id"):
            if tool_name in ("trash_folder", "restore_folder", "delete_folder") and resolved.get("folder_name"):
                pass
            else:
                entity = ctx.get_entity("folder")
                if entity:
                    resolved["folder_id"] = entity.id
                else:
                    missing.append("folder_id")

    return resolved, missing


def execute_tool_safe(
    mcp,
    tool_name: str,
    args: dict,
    ctx: ContextState
) -> tuple[dict, str]:
    """
    Execute tool with argument resolution and validation.
    Returns: (result_dict, error_tag)  — error_tag is "" on success.
    """
    resolved_args, missing = resolve_arguments(tool_name, args, ctx)
    resolved_args = _apply_arg_aliases(tool_name, resolved_args)

    if ctx.pending_attachment_path and tool_name in (
        "send_email", "draft_email", "send_email_with_attachment"
    ):
        resolved_args.setdefault("attachment_path", ctx.pending_attachment_path)

    if missing:
        return {
            "success": False,
            "error": f"Missing required field(s): {', '.join(missing)}. Please specify which item you mean."
        }, "needs_clarification"

    validation_error = validate_tool_arguments(tool_name, resolved_args)
    if validation_error:
        return {"success": False, "error": validation_error}, "validation_failed"

    try:
        result = mcp.execute_tool(tool_name, resolved_args)
        if result and result.get("success"):
            update_context_from_tool_result(ctx, tool_name, result, resolved_args)
        return result, ""
    except Exception as e:
        logger.exception(f"Tool execution failed: {tool_name}")
        return {"success": False, "error": str(e)}, "execution_failed"


# Argument aliases: maps wrong param names (from LLM or strict-path) → correct ones.
# Keyed by tool name so renaming is scoped and doesn't collide across tools.
_TOOL_ARG_ALIASES: Dict[str, Dict[str, str]] = {
    "get_emails_by_date_range": {"start_date": "start", "end_date": "end"},
    "get_emails_by_label":      {"label_name": "label"},
    "add_label":                {"label_name": "label"},
    "remove_label":             {"label_name": "label"},
    "read_sheet":               {"range": "range_name"},
    "write_to_sheet":           {"range": "range_name"},
    "append_to_sheet":          {"range": "range_name"},
    "clear_sheet_range":        {"range": "range_name"},
    "replace_text_in_doc":      {"old_text": "find", "new_text": "replace"},
    "move_file":                {"folder_id": "destination_folder_id"},
    "upload_file":              {"parent_id": "folder_id"},
}


def _apply_arg_aliases(tool_name: str, args: dict) -> dict:
    """Rename any mismatched argument keys for the given tool."""
    aliases = _TOOL_ARG_ALIASES.get(tool_name, {})
    if not aliases:
        return args
    result = dict(args)
    for old_key, new_key in aliases.items():
        if old_key in result and new_key not in result:
            result[new_key] = result.pop(old_key)
    # append_to_sheet requires range_name — default to "A1" if still missing
    if tool_name == "append_to_sheet" and "range_name" not in result:
        result["range_name"] = "A1"
    return result


def validate_tool_arguments(_tool_name: str, args: dict) -> str:
    """Basic argument validation."""
    if "to" in args:
        email_val = args.get("to", "")
        if email_val and "@" not in email_val:
            return f"Invalid email address: {email_val}"
    return ""


# ============================================================================
# SECTION 4: HELPERS + STRICT INTENT DETECTION
# ============================================================================

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}')

_STOP_WORDS = frozenset({
    "trash", "inbox", "drafts", "sent", "spam", "me", "my", "the", "it",
    "them", "all", "please", "now", "this", "that", "here", "there",
    "latest", "last", "first", "new", "old", "recent", "email", "mail",
    "message", "hi", "hello", "thanks", "ok", "sure", "yes", "no",
    "a", "an", "and", "or", "of", "to", "for", "is", "in", "on",
    "at", "by", "up", "do", "go", "get", "give", "show", "read",
    "check", "find", "search", "help", "can", "you", "i", "we",
    "one", "any", "some", "just", "about", "with", "from", "reply",
    "forward", "delete", "send", "write", "compose", "draft", "make",
    "create", "open", "see", "view", "look", "fetch", "retrieve",
})


def _extract_email_addr(text: str) -> Optional[str]:
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else None


def _is_real_recipient(word: str) -> bool:
    """True if word looks like a real email or username, not a common stop-word."""
    if not word or len(word) < 2:
        return False
    if word.lower() in _STOP_WORDS:
        return False
    if "@" in word:
        return bool(_EMAIL_RE.match(word))
    return bool(re.match(r'^[a-zA-Z][\w.\-]{2,}$', word))


def _position_to_index(pos: str) -> int:
    """Convert 'first'/'1st'/1 → 0-based index."""
    pos = pos.lower().strip()
    mapping = {
        "first": 0, "1st": 0, "one": 0,
        "second": 1, "2nd": 1, "two": 1,
        "third": 2, "3rd": 2, "three": 2,
        "fourth": 3, "4th": 3,
        "fifth": 4, "5th": 4,
        "last": -1, "latest": 0, "newest": 0, "most recent": 0,
    }
    if pos in mapping:
        return mapping[pos]
    try:
        return int(pos) - 1
    except ValueError:
        return 0


def _greeting_msg(mode: str) -> str:
    base = "👋 <b>Hi! I'm G-Assistant.</b> Here's what I can do in <b>{mode}</b> mode:<br><br>"
    mode_help = {
        "gmail": (
            base.format(mode="Gmail") +
            "• <b>Read</b>: show inbox · check unread · show starred<br>"
            "• <b>Search</b>: search emails for [keyword] · emails from [sender]<br>"
            "• <b>Send</b>: send email to X saying Y<br>"
            "• <b>Draft</b>: create a draft to X saying Y · send it<br>"
            "• <b>Organise</b>: delete / archive / star / label emails<br>"
            "• <b>AI</b>: summarize email · extract tasks · detect urgency<br><br>"
            "Switch modes using the sidebar. Just tell me what you need!"
        ),
        "docs": (
            base.format(mode="Docs") +
            "• <b>Browse</b>: list docs · search docs for [keyword]<br>"
            "• <b>Create</b>: create doc titled [name]<br>"
            "• <b>Edit</b>: append to it · replace text · rename it<br>"
            "• <b>View</b>: open [doc name] · show its content<br>"
        ),
        "sheets": (
            base.format(mode="Sheets") +
            "• <b>Browse</b>: list sheets · search sheets for [keyword]<br>"
            "• <b>Create</b>: create sheet titled [name]<br>"
            "• <b>Edit</b>: write to it · append a row · read range A1:C10<br>"
        ),
        "drive": (
            base.format(mode="Drive") +
            "• <b>Browse</b>: list files · list folders · recent files · storage<br>"
            "• <b>Search</b>: search files for [keyword]<br>"
            "• <b>Organise</b>: create folder · rename · move · share · trash<br>"
        ),
    }
    return mode_help.get(mode, base.format(mode=mode) + "Ask me anything!")


def _help_msg(mode: str) -> str:
    return _greeting_msg(mode)


# ── Smart history helpers ────────────────────────────────────────────────────

def _summarize_tool_result(tool_name: str, res: dict) -> str:
    """Compact history entry for LLM context — preserves key IDs without full JSON."""
    if not isinstance(res, dict):
        return f"Tool({tool_name}): {str(res)[:200]}"
    if not res.get("success", True):
        return f"Tool({tool_name}): ERROR — {res.get('error', 'unknown')}"

    data = res.get("result", res)

    if isinstance(data, list) and data:
        sample = data[0] if isinstance(data[0], dict) else {}
        ids = [e.get("id", "") for e in data if isinstance(e, dict) and e.get("id")]
        count = len(data)
        if "subject" in sample or "from" in sample:
            subjects = [e.get("subject", "") for e in data[:3]]
            id_str = ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")
            return (f"Tool({tool_name}): {count} email(s). "
                    f"IDs:[{id_str}]. Subjects: {'; '.join(subjects)}")
        if "title" in sample:
            titles = [e.get("title", "") for e in data[:3]]
            id_str = ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")
            return f"Tool({tool_name}): {count} item(s). IDs:[{id_str}]. Titles: {'; '.join(titles)}"
        if "name" in sample:
            names = [e.get("name", "") for e in data[:3]]
            id_str = ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")
            return f"Tool({tool_name}): {count} item(s). IDs:[{id_str}]. Names: {'; '.join(names)}"
        return f"Tool({tool_name}): {count} item(s). IDs:[{', '.join(ids[:5])}]"

    if isinstance(data, dict):
        eid = data.get("id", "")
        extra = ""
        if "subject" in data:
            extra = f", subject:{data['subject']}"
        if "to" in data:
            extra += f", to:{data['to']}"
        if "title" in data:
            extra = f", title:{data['title']}"
        if "name" in data:
            extra = f", name:{data['name']}"
        return f"Tool({tool_name}): success. ID:{eid}{extra}"

    return f"Tool({tool_name}): {str(data)[:200]}"


def _trim_history_smart(history: list, max_turns: int = 30) -> list:
    """
    Trim history keeping system message + most recent turns.
    Drops oldest user/assistant pairs cleanly and injects a one-line
    summary of dropped content so the LLM retains key facts.
    """
    if len(history) <= max_turns:
        return history

    system_msg = history[0]
    body = history[1:]

    # Skip existing summary if present
    summary_offset = 0
    if body and body[0].get("role") == "system" and \
            isinstance(body[0].get("content", ""), str) and \
            body[0]["content"].startswith("Earlier:"):
        summary_offset = 1

    turns = body[summary_offset:]
    budget = max_turns - 2  # room for system + summary

    if len(turns) <= budget:
        return [system_msg] + body

    keep_start = len(turns) - budget
    # Align to a user turn boundary
    while keep_start < len(turns) and turns[keep_start].get("role") != "user":
        keep_start += 1

    dropped = turns[:keep_start]
    kept = turns[keep_start:]

    # Build compact summary from dropped turns
    facts = []
    i = 0
    while i < len(dropped):
        msg = dropped[i]
        if msg.get("role") == "user":
            utxt = str(msg.get("content", ""))[:80]
            atxt = ""
            if i + 1 < len(dropped) and dropped[i + 1].get("role") == "assistant":
                atxt = str(dropped[i + 1].get("content", ""))[:80]
                i += 1
            facts.append(f'U:"{utxt}" → A:"{atxt}"' if atxt else f'U:"{utxt}"')
        i += 1

    result = [system_msg]
    if facts:
        result.append({"role": "system",
                        "content": "Earlier: " + " | ".join(facts)})
    result.extend(kept)
    return result


def intent_detect_strict(
    user_input: str,
    mcp,
    state: dict
) -> Optional[str]:
    """
    Deterministic intent detection — handles only unambiguous commands
    with no pronouns or context references.

    Returns HTML string if handled, None to fall through to LLM.
    """
    low = user_input.lower().strip()
    ctx = get_or_create_context(state)
    mode = ctx.active_mode

    # ── Auth (always) ─────────────────────────────────────────────────────────
    if any(k in low for k in ("authenticate gmail", "sign in to gmail", "connect gmail")):
        return "Please run <b>python auth.py</b> in your terminal to authenticate."

    # ── Greeting (always) ─────────────────────────────────────────────────────
    if re.match(r'^(hi+|hello+|hey+)[!.\s]*$', low):
        return _greeting_msg(mode)

    # ── Help / capabilities ───────────────────────────────────────────────────
    if re.search(r'\b(what\s+can\s+you\s+do|help|capabilities|commands)\b', low) and len(low) < 60:
        return _help_msg(mode)

    # ── Cancel pending ────────────────────────────────────────────────────────
    if low.strip() in ("cancel", "nevermind", "never mind", "stop", "abort"):
        ctx.pending_action = None
        return "Cancelled. What else can I help you with?"

    # ══════════════════════════════════════════════════════════════════════════
    # GMAIL MODE
    # ══════════════════════════════════════════════════════════════════════════
    if mode == "gmail":

        # "Emails from <sender>" — sender search
        from_match = re.search(r'\bemails?\s+from\s+(\S+)', low)
        if from_match:
            sender = from_match.group(1).rstrip(".,:;!?")
            if _is_real_recipient(sender):
                return _run_and_fmt(mcp, "search_emails", {"query": f"from:{sender}"}, ctx)

        # Date shortcuts
        _today = datetime.date.today()
        if re.search(r"\btoday'?s?\s+emails?\b|\bemails?\s+(?:from\s+)?today\b", low):
            d = _today.strftime("%Y/%m/%d")
            return _run_and_fmt(mcp, "get_emails_by_date_range",
                                {"start": d, "end": d}, ctx)
        if re.search(r"\byesterday'?s?\s+emails?\b|\bemails?\s+(?:from\s+)?yesterday\b", low):
            _yd = (_today - datetime.timedelta(days=1)).strftime("%Y/%m/%d")
            return _run_and_fmt(mcp, "get_emails_by_date_range",
                                {"start": _yd, "end": _yd}, ctx)
        if re.search(r"\blast\s+week'?s?\s+emails?\b|\bemails?\s+(?:from\s+)?last\s+week\b", low):
            _start = (_today - datetime.timedelta(days=7)).strftime("%Y/%m/%d")
            _end = _today.strftime("%Y/%m/%d")
            return _run_and_fmt(mcp, "get_emails_by_date_range",
                                {"start": _start, "end": _end}, ctx)

        # "Send it" / "send that" — send the last saved draft
        if re.match(r'^send\s+(it|that|the\s+draft)\.?\s*$', low) and ctx.last_draft_id:
            return _run_and_fmt(mcp, "send_draft", {"draft_id": ctx.last_draft_id}, ctx)

        # Resend / send again — repeat last sent email
        if re.search(r'\b(resend|send\s+again|send\s+it\s+again)\b', low):
            if ctx.last_to and ctx.last_body:
                return _run_and_fmt(mcp, "send_email", {
                    "to": ctx.last_to,
                    "subject": ctx.last_subject or "Hello from G-Assistant",
                    "body": ctx.last_body,
                }, ctx)

        # Position-based open: "open the first email", "show the second one"
        pos_open = re.search(
            r'\b(?:open|read|show|view)\s+(?:the\s+)?'
            r'(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last|latest)\s*'
            r'(?:one|email|mail|message)?\b', low
        )
        if pos_open and ctx.last_viewed_ids:
            idx = _position_to_index(pos_open.group(1))
            try:
                return _run_and_fmt(mcp, "get_email_by_id",
                                    {"message_id": ctx.last_viewed_ids[idx]}, ctx)
            except IndexError:
                pass

        # Position-based delete: "delete the second email"
        pos_del = re.search(
            r'\b(?:delete|trash|remove)\s+(?:the\s+)?'
            r'(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th|last|latest)\s*'
            r'(?:one|email|mail|message)?\b', low
        )
        if pos_del and ctx.last_viewed_ids:
            idx = _position_to_index(pos_del.group(1))
            try:
                return _run_and_fmt(mcp, "trash_email",
                                    {"message_id": ctx.last_viewed_ids[idx]}, ctx)
            except IndexError:
                pass

        # "Delete this" / "delete that" — trash most recently viewed email
        if re.match(r'^(?:delete|trash|remove)\s+(?:this|that)\.?\s*$', low):
            target_id = (ctx.last_viewed_ids[0] if ctx.last_viewed_ids else
                         (ctx.get_entity("email").id if ctx.get_entity("email") else None))
            if target_id:
                return _run_and_fmt(mcp, "trash_email", {"message_id": target_id}, ctx)

        # Bulk delete: "delete all [emails]"
        if re.match(r'^delete\s+all\b', low) and ctx.last_viewed_ids:
            ids_to_delete = list(ctx.last_viewed_ids)
            ok = 0
            for mid in ids_to_delete:
                r, _ = execute_tool_safe(mcp, "trash_email", {"message_id": mid}, ctx)
                if r.get("success"):
                    ok += 1
            ctx.last_viewed_ids = []
            return f"✅ Moved {ok}/{len(ids_to_delete)} emails to trash."

        # Draft creation — must be caught before any "send" pattern
        draft_create = re.search(
            r'\b(?:create\s+(?:a\s+)?draft|draft\s+(?:an?\s+)?(?:email|mail|message)|save\s+(?:as\s+)?draft)\s+'
            r'(?:to\s+)?([\w\.\-\+]+@[\w\.-]+\.\w+)'
            r'(?:\s+(?:saying|with(?:\s+body)?|about)\s+(.+))?',
            low
        )
        if draft_create:
            to_email = draft_create.group(1)
            body = (draft_create.group(2) or "Hello").strip()
            return _run_and_fmt(mcp, "draft_email",
                                {"to": to_email, "subject": "(Draft)", "body": body}, ctx)

        # Send email — explicit send keyword
        send_create = re.search(
            r'\b(?:send|mail)\s+(?:an?\s+)?(?:email|mail|message)\s+to\s+([\w\.\-\+]+@[\w\.-]+\.\w+)'
            r'(?:\s+(?:saying|with(?:\s+body)?|about)\s+(.+))?',
            low
        )
        if send_create:
            to_email = send_create.group(1)
            body = (send_create.group(2) or "Hello").strip()
            return _run_and_fmt(mcp, "send_email",
                                {"to": to_email, "subject": "Hello from G-Assistant", "body": body}, ctx)

        if re.search(r'\b(?:show|list|get|display|fetch)\s+(?:my\s+)?inbox\b', low):
            return _run_and_fmt(mcp, "get_emails", {"limit": 10}, ctx)

        if re.search(r'\b(?:show|check|get)\s+(?:my\s+)?unread\b', low):
            return _run_and_fmt(mcp, "get_unread_emails", {"limit": 10}, ctx)

        if re.search(r'\b(?:show|list)\s+(?:my\s+)?starred\b', low):
            return _run_and_fmt(mcp, "get_starred_emails", {"limit": 10}, ctx)

        if re.search(r'\b(?:list|show)\s+(?:all\s+)?labels\b', low):
            return _run_and_fmt(mcp, "list_labels", {}, ctx)

        search_match = (
            re.search(r'\bsearch\s+for\s+"([^"]+)"', low) or
            re.search(r"\bsearch\s+for\s+'([^']+)'", low) or
            re.search(r'\bsearch\s+(?:emails?\s+)?for\s+(\S.*)', low)
        )
        if search_match:
            query = search_match.group(1).strip()
            if query not in ("this", "that", "it", "the one"):
                return _run_and_fmt(mcp, "search_emails", {"query": query}, ctx)

        view_match = re.search(
            r'\b(?:open|read|show|view)\s+(?:email\s+)?([a-fA-F0-9]{10,})\b', low
        )
        if view_match:
            return _run_and_fmt(mcp, "get_email_by_id", {"message_id": view_match.group(1)}, ctx)

        delete_match = re.search(
            r'\b(?:delete|trash|remove)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low
        )
        if delete_match:
            return _run_and_fmt(mcp, "trash_email", {"message_id": delete_match.group(1)}, ctx)

        draft_view = re.search(r'\b(?:open|read|show)\s+draft\s+(r\d+)\b', low)
        if draft_view:
            return _run_and_fmt(mcp, "get_email_by_id", {"message_id": draft_view.group(1)}, ctx)

        draft_delete = re.search(r'\b(?:delete|remove)\s+draft\s+(r\d+)\b', low)
        if draft_delete:
            return _run_and_fmt(mcp, "delete_draft", {"draft_id": draft_delete.group(1)}, ctx)

        send_draft_match = (
            re.search(r'\bsend\s+draft\s+(r\d+)\b', low) or
            re.search(r'\b(r\d+)\s*(?:->|=>)\s*send\b', low)
        )
        if send_draft_match:
            return _run_and_fmt(mcp, "send_draft", {"draft_id": send_draft_match.group(1)}, ctx)

        reply_match = re.search(
            r'\b(?:reply to|respond to)\s+(?:email\s+)?([a-fA-F0-9]{10,})\s+saying\s+(.+)', low
        )
        if reply_match:
            msg_id, body = reply_match.groups()
            return _run_and_fmt(mcp, "reply_email",
                                {"message_id": msg_id, "body": body.strip()}, ctx)

        forward_match = re.search(
            r'\bforward\s+(?:email\s+)?([a-fA-F0-9]{10,})\s+to\s+([\w\.-]+@[\w\.-]+\.\w+)\b', low
        )
        if forward_match:
            msg_id, to_email = forward_match.groups()
            return _run_and_fmt(mcp, "forward_email",
                                {"message_id": msg_id, "to": to_email}, ctx)

        star_match = re.search(r'\b(?:star|flag)\s+(?:email\s+)?([a-fA-F0-9]{10,})\b', low)
        if star_match:
            return _run_and_fmt(mcp, "star_email", {"message_id": star_match.group(1)}, ctx)

        archive_match = re.search(r'\barchive\s+(?:email\s+)?([a-fA-F0-9]{10,})\b', low)
        if archive_match:
            return _run_and_fmt(mcp, "archive_email", {"message_id": archive_match.group(1)}, ctx)

        thread_match = re.search(r'\bthread\s+([a-fA-F0-9]{10,})\b', low)
        if thread_match:
            return _run_and_fmt(mcp, "get_email_thread",
                                {"thread_id": thread_match.group(1)}, ctx)

        label_add = re.search(
            r'\b(?:add|apply)\s+(?:label\s+)?"?([^"]+)"?\s+(?:to|on)\s+(?:email\s+)?([a-fA-F0-9]{10,})\b',
            low
        )
        if label_add:
            label, msg_id = label_add.groups()
            return _run_and_fmt(mcp, "add_label",
                                {"message_id": msg_id, "label": label.strip()}, ctx)

    # ══════════════════════════════════════════════════════════════════════════
    # DOCS MODE
    # ══════════════════════════════════════════════════════════════════════════
    elif mode == "docs":

        if re.search(r'\b(?:show|list|get|display)\s+(?:my\s+)?docs?\b', low):
            return _run_and_fmt(mcp, "list_docs", {"limit": 10}, ctx)

        search_match = re.search(r'\bsearch\s+(?:docs?\s+)?for\s+(.+)', low)
        if search_match:
            query = search_match.group(1).strip()
            if query not in ("this", "that", "it"):
                return _run_and_fmt(mcp, "search_docs", {"query": query}, ctx)

        create_match = re.search(
            r'\bcreate\s+(?:a\s+)?(?:new\s+)?doc(?:ument)?\s+(?:titled?|called|named)\s+"?(.+?)"?\s*$',
            low
        )
        if create_match:
            title = create_match.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "create_doc", {"title": title}, ctx)

        # Rename current doc: "rename it to X" / "rename to X" / "rename doc to X"
        rename_doc = re.search(
            r'\b(?:rename|call|title)\s+(?:it|this|the\s+doc(?:ument)?)?\s*'
            r'(?:to|as)\s+"?(.+?)"?\s*$', low
        )
        if rename_doc and ctx.get_entity("doc"):
            new_title = rename_doc.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "update_doc_title", {"new_title": new_title}, ctx)

        # Open / view current doc
        if re.match(r'^(?:open|show|view|read|get)\s+(?:it|this|the\s+doc(?:ument)?)\s*$', low):
            if ctx.get_entity("doc"):
                return _run_and_fmt(mcp, "get_doc", {}, ctx)

        # Append to current doc
        append_doc = re.search(
            r'\b(?:append|add|insert)\s+(?:to\s+(?:it|the\s+doc(?:ument)?)\s+)?[:\-]?\s*(.+)', low
        )
        if append_doc and ctx.get_entity("doc"):
            text = append_doc.group(1).strip()
            if len(text) > 3:
                return _run_and_fmt(mcp, "append_to_doc", {"text": text}, ctx)

        # Delete current doc
        if re.match(
            r'^(?:delete|trash|remove)\s+(?:it|this|the\s+doc(?:ument)?)\s*$', low
        ) and ctx.get_entity("doc"):
            return _run_and_fmt(mcp, "delete_doc", {}, ctx)

    # ══════════════════════════════════════════════════════════════════════════
    # SHEETS MODE
    # ══════════════════════════════════════════════════════════════════════════
    elif mode == "sheets":

        if re.search(r'\b(?:show|list|get|display)\s+(?:my\s+)?(?:spread)?sheets?\b', low):
            return _run_and_fmt(mcp, "list_sheets", {"limit": 10}, ctx)

        search_match = re.search(r'\bsearch\s+(?:sheets?\s+)?for\s+(.+)', low)
        if search_match:
            query = search_match.group(1).strip()
            if query not in ("this", "that", "it"):
                return _run_and_fmt(mcp, "search_sheets", {"query": query}, ctx)

        create_match = re.search(
            r'\bcreate\s+(?:a\s+)?(?:new\s+)?(?:spread)?sheet\s+(?:titled?|called|named)\s+"?(.+?)"?\s*$',
            low
        )
        if create_match:
            title = create_match.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "create_sheet", {"title": title}, ctx)

        # Open / view current sheet
        if re.match(r'^(?:open|show|view|get)\s+(?:it|this|the\s+sheet)\s*$', low):
            if ctx.get_entity("sheet"):
                return _run_and_fmt(mcp, "get_sheet", {}, ctx)

        # Delete current sheet
        if re.match(
            r'^(?:delete|trash|remove)\s+(?:it|this|the\s+(?:spread)?sheet)\s*$', low
        ) and ctx.get_entity("sheet"):
            return _run_and_fmt(mcp, "delete_sheet", {}, ctx)

    # ══════════════════════════════════════════════════════════════════════════
    # DRIVE MODE
    # ══════════════════════════════════════════════════════════════════════════
    elif mode == "drive":

        if re.search(r'\b(?:show|list|get)\s+(?:my\s+)?files?\b', low):
            return _run_and_fmt(mcp, "list_files", {"limit": 10}, ctx)

        if re.search(r'\b(?:show|list|get)\s+(?:my\s+)?folders?\b', low):
            return _run_and_fmt(mcp, "list_folders", {"limit": 10}, ctx)

        if re.search(r'\b(?:show|get|list)\s+(?:my\s+)?recent\s+files?\b', low):
            return _run_and_fmt(mcp, "get_recent_files", {"limit": 10}, ctx)

        if re.search(r'\b(?:show|get|list)\s+(?:my\s+)?starred\s+files?\b', low):
            return _run_and_fmt(mcp, "get_starred_files", {"limit": 10}, ctx)

        if re.search(r'\b(?:storage|quota|disk\s+space)\b', low):
            return _run_and_fmt(mcp, "get_storage_info", {}, ctx)

        search_match = re.search(r'\bsearch\s+(?:files?\s+)?for\s+(.+)', low)
        if search_match:
            query = search_match.group(1).strip()
            if query not in ("this", "that", "it"):
                return _run_and_fmt(mcp, "search_files", {"query": query}, ctx)

        create_folder_match = re.search(
            r'\bcreate\s+(?:a\s+)?(?:new\s+)?folder\s+(?:named?|called)\s+"?(.+?)"?\s*$',
            low
        )
        if create_folder_match:
            name = create_folder_match.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "create_folder", {"name": name}, ctx)

        # Trash folder by name: "delete folder called hello1" / "trash folder named foo"
        delete_folder_match = re.search(
            r'\b(?:delete|trash|remove)\s+(?:a\s+)?(?:the\s+)?folder\s+(?:named?|called)\s+"?(.+?)"?\s*$',
            low
        )
        if delete_folder_match:
            name = delete_folder_match.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "delete_folder", {"folder_name": name}, ctx)

        # Restore folder by name: "restore folder called hello1"
        restore_folder_match = re.search(
            r'\brestore\s+(?:a\s+)?(?:the\s+)?folder\s+(?:named?|called)\s+"?(.+?)"?\s*$',
            low
        )
        if restore_folder_match:
            name = restore_folder_match.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "restore_folder", {"folder_name": name}, ctx)

        # "Create folder X and upload/put [attachment] in it" — two-step atomic
        create_upload = re.search(
            r'\bcreate\s+(?:a\s+)?(?:new\s+)?folder\s+(?:named?|called)?\s*"?([^"]+?)"?'
            r'\s+and\s+(?:put|upload|add|move)\s+(?:the\s+)?(?:attachment|file|it|this|png|image)\b',
            low
        )
        if create_upload and ctx.pending_attachment_path:
            folder_name = create_upload.group(1).strip().strip('"\'')
            folder_res, _ = execute_tool_safe(mcp, "create_folder", {"name": folder_name}, ctx)
            if not folder_res.get("success"):
                return format_tool_result("create_folder", folder_res)
            folder_id = (folder_res.get("result") or {}).get("id", "")
            if not folder_id:
                return "<b style='color:#ef4444'>Error:</b> Folder created but ID missing."
            upload_res, _ = execute_tool_safe(mcp, "upload_file", {
                "file_path": ctx.pending_attachment_path,
                "folder_id": folder_id,
            }, ctx)
            if not upload_res.get("success"):
                err = upload_res.get("error", "unknown")
                return (f"✅ Folder <b>{folder_name}</b> created "
                        f"(ID: <code>{folder_id}</code>)<br>"
                        f"<b style='color:#ef4444'>Upload failed:</b> {err}")
            fd = upload_res.get("result") or {}
            fn = fd.get("name", "file")
            fid = fd.get("id", "")
            url = fd.get("url", "")
            link = f'<a href="{url}" target="_blank">{fn}</a>' if url else fn
            return (
                f"✅ Folder <b>{folder_name}</b> created &amp; file uploaded!<br><br>"
                f"<b>File:</b> {link}<br>"
                f"<small>File ID: <code>{fid}</code> &nbsp; "
                f"Folder ID: <code>{folder_id}</code></small>"
            )

        # ── Attachment upload (all variants handled here) ────────────────────
        if ctx.pending_attachment_path:
            folder_name = _extract_folder_name(user_input)

            if folder_name:
                # Named folder → find it, then upload
                try:
                    folder = find_folder_by_name(folder_name)
                except Exception:
                    folder = None
                if folder:
                    fid = folder.get("id", "")
                    ctx.set_entity("folder", Entity(id=fid, type="folder",
                                                    attributes={"name": folder_name}))
                    return _upload_to_folder(mcp, ctx, fid, folder_name)
                return (f"<b style='color:#ef4444'>Error:</b> No folder named "
                        f"<b>{folder_name}</b> found in Drive.")

            # "that folder" / "this folder" / "in it" → use context
            clean_low = re.sub(r'\[(?:user\s+)?attached[^\]]*\]', '', low,
                               flags=re.IGNORECASE).strip()
            if re.search(r'\b(that|this|the)\s+folder\b|\bin\s+it\b|\bthere\b', clean_low):
                folder_entity = ctx.get_entity("folder")
                if folder_entity:
                    fname = folder_entity.attributes.get("name", "selected folder")
                    return _upload_to_folder(mcp, ctx, folder_entity.id, fname)

            # Plain upload (no folder specified)
            if re.search(r'\b(?:upload|save|put|add|attach)\b', clean_low):
                return _run_and_fmt(mcp, "upload_file",
                                    {"file_path": ctx.pending_attachment_path}, ctx)

        # Rename current file: "rename it to X"
        rename_file_match = re.search(
            r'\b(?:rename|call)\s+(?:it|this|the\s+file)?\s*(?:to|as)\s+"?(.+?)"?\s*$', low
        )
        if rename_file_match and ctx.get_entity("file"):
            new_name = rename_file_match.group(1).strip().strip('"\'')
            return _run_and_fmt(mcp, "rename_file", {"new_name": new_name}, ctx)

        # Trash current file: "trash it" / "delete it" / "delete that file"
        if re.match(
            r'^(?:delete|trash|remove)\s+(?:it|this|that|the\s+file|that\s+file)\s*$', low
        ) and ctx.get_entity("file"):
            return _run_and_fmt(mcp, "trash_file", {}, ctx)

        # Trash current folder: "delete this folder" / "trash it" (when folder is active)
        if re.match(
            r'^(?:delete|trash|remove)\s+(?:it|this|that|the\s+folder|that\s+folder)\s*$', low
        ) and ctx.get_entity("folder"):
            return _run_and_fmt(mcp, "trash_folder", {}, ctx)

    # Anything with pronouns or ambiguity → fall through to LLM
    return None


_FOLDER_NAME_EXCLUDE = frozenset({
    "the", "a", "an", "it", "this", "that", "these", "those",
    "my", "me", "i", "we", "you", "in", "on", "at", "to", "for",
    "of", "and", "or", "is", "be", "do",
})


def _extract_folder_name(text: str) -> Optional[str]:
    """Extract a folder name from a user message using multiple flexible patterns."""
    # Strip [user attached image: ...] / [attached ...] annotations before parsing
    clean = re.sub(r'\[(?:user\s+)?attached[^\]]*\]', '', text, flags=re.IGNORECASE).strip()

    patterns = [
        r'\bfolder\s+(?:called|named)\s+"?([^"\n\[\]/\\]{1,60}?)"?(?:\s*$|\s+and\b)',
        r'\bin\s+(?:the\s+)?"?([^"\n\[\]/\\]{1,60}?)"?\s+folder\b',
        r'\bto\s+(?:the\s+)?"?([^"\n\[\]/\\]{1,60}?)"?\s+folder\b',
        r'"([^"\n\[\]]{1,60})"\s+folder\b',
        r'\bthe\s+([^\s\[\]]{1,40})\s+folder\b',
    ]
    for pat in patterns:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            name = m.group(1).strip().strip('"\'').rstrip('.,;')
            if name and len(name) > 1 and name.lower() not in _FOLDER_NAME_EXCLUDE:
                return name
    return None


def _run_and_fmt(mcp, tool_name: str, args: dict, ctx: ContextState) -> str:
    """Execute a tool and return formatted HTML output."""
    result, _ = execute_tool_safe(mcp, tool_name, args, ctx)
    return format_tool_result(tool_name, result)


def _upload_to_folder(mcp, ctx: ContextState, folder_id: str, folder_name: str) -> str:
    """Upload ctx.pending_attachment_path into a known folder and return formatted HTML."""
    upload_res, _ = execute_tool_safe(mcp, "upload_file", {
        "file_path": ctx.pending_attachment_path,
        "folder_id": folder_id,
    }, ctx)
    if not upload_res.get("success"):
        err = upload_res.get("error", "unknown error")
        return f"<b style='color:#ef4444'>Upload failed:</b> {err}"
    fd = upload_res.get("result") or {}
    fn  = fd.get("name", "file")
    fid = fd.get("id", "")
    url = fd.get("url", "")
    link = f'<a href="{url}" target="_blank">{fn}</a>' if url else fn
    return (
        f"✅ Uploaded to folder <b>{folder_name}</b>!<br><br>"
        f"<b>File:</b> {link}<br>"
        f"<small>File ID: <code>{fid}</code> &nbsp; "
        f"Folder ID: <code>{folder_id}</code></small>"
    )


# ── Per-type HTML helpers ────────────────────────────────────────────────────

def _fmt_email_list(emails) -> str:
    if not isinstance(emails, list) or not emails:
        return "No emails found."
    lines = [f"<b>Found {len(emails)} email(s):</b><br><br>"]
    for i, e in enumerate(emails, 1):
        subj = e.get("subject", "(no subject)")
        frm = e.get("from", "")
        date = e.get("date", "")[:25]
        eid = e.get("id", "")
        snippet = e.get("snippet", "")[:80]
        lines.append(
            f"<b>{i}. {subj}</b><br>"
            f"&nbsp;&nbsp;From: {frm}&nbsp;&nbsp;{date}<br>"
            f"&nbsp;&nbsp;<i>{snippet}…</i><br>"
            f"&nbsp;&nbsp;<small>ID: <code>{eid}</code></small><br><br>"
        )
    return "".join(lines)


def _fmt_single_email(email) -> str:
    if not isinstance(email, dict):
        return str(email)
    frm  = email.get("from", "")
    to   = email.get("to", "")
    subj = email.get("subject", "(no subject)")
    date = email.get("date", "")
    body = email.get("body", "")[:3000]
    eid  = email.get("id", "")
    return (
        f"<b>From:</b> {frm}<br>"
        f"<b>To:</b> {to}<br>"
        f"<b>Subject:</b> {subj}<br>"
        f"<b>Date:</b> {date}<br>"
        f"<small><b>ID:</b> <code>{eid}</code></small>"
        f"<hr style='border:none;border-top:1px solid #444;margin:8px 0'>"
        f"<p style='white-space:pre-wrap'>{body}</p>"
    )


def _fmt_label_list(data) -> str:
    labels = data if isinstance(data, list) else data.get("labels", []) if isinstance(data, dict) else []
    if not labels:
        return "No labels found."
    lines = [f"<b>Labels ({len(labels)}):</b><br>"]
    for lbl in labels:
        name = lbl.get("name", lbl.get("id", "")) if isinstance(lbl, dict) else str(lbl)
        lines.append(f"• {name}<br>")
    return "".join(lines)


def _fmt_thread(data) -> str:
    emails = data if isinstance(data, list) else []
    if not emails:
        return "Empty thread."
    lines = [f"<b>Thread — {len(emails)} message(s):</b><br><br>"]
    for i, e in enumerate(emails, 1):
        frm     = e.get("from", "")
        date    = e.get("date", "")[:25]
        snippet = e.get("snippet", "")[:120]
        lines.append(f"<b>{i}.</b> <b>{frm}</b> — {date}<br>&nbsp;&nbsp;{snippet}…<br><br>")
    return "".join(lines)


def _fmt_doc_list(docs) -> str:
    if not isinstance(docs, list) or not docs:
        return "No documents found."
    lines = [f"<b>Found {len(docs)} document(s):</b><br><br>"]
    for i, d in enumerate(docs, 1):
        title    = d.get("title", "(Untitled)")
        url      = d.get("url", "")
        modified = d.get("modified", "")[:10]
        doc_id   = d.get("id", "")
        link = f'<a href="{url}" target="_blank">{title}</a>' if url else title
        lines.append(
            f"<b>{i}.</b> {link}<br>"
            f"&nbsp;&nbsp;<small>Modified: {modified}&nbsp;&nbsp;ID: <code>{doc_id}</code></small><br><br>"
        )
    return "".join(lines)


def _fmt_single_doc(doc) -> str:
    if not isinstance(doc, dict):
        return str(doc)
    title  = doc.get("title", "(Untitled)")
    url    = doc.get("url", "")
    text   = doc.get("text", "")[:3000]
    doc_id = doc.get("id", "")
    link   = f'<a href="{url}" target="_blank">Open in Google Docs ↗</a>' if url else ""
    return (
        f"<b>{title}</b>&nbsp;&nbsp;{link}<br>"
        f"<small>ID: <code>{doc_id}</code></small>"
        f"<hr style='border:none;border-top:1px solid #444;margin:8px 0'>"
        f"<p style='white-space:pre-wrap'>{text}</p>"
    )


def _fmt_sheet_list(sheets) -> str:
    if not isinstance(sheets, list) or not sheets:
        return "No spreadsheets found."
    lines = [f"<b>Found {len(sheets)} spreadsheet(s):</b><br><br>"]
    for i, s in enumerate(sheets, 1):
        title    = s.get("title", "(Untitled)")
        url      = s.get("url", "")
        modified = s.get("modified", "")[:10]
        sid      = s.get("id", "")
        link = f'<a href="{url}" target="_blank">{title}</a>' if url else title
        lines.append(
            f"<b>{i}.</b> {link}<br>"
            f"&nbsp;&nbsp;<small>Modified: {modified}&nbsp;&nbsp;ID: <code>{sid}</code></small><br><br>"
        )
    return "".join(lines)


def _fmt_single_sheet(sheet) -> str:
    if not isinstance(sheet, dict):
        return str(sheet)
    title = sheet.get("title", "(Untitled)")
    url   = sheet.get("url", "")
    sid   = sheet.get("id", "")
    tabs  = sheet.get("tabs", [])
    link  = f'<a href="{url}" target="_blank">Open in Google Sheets ↗</a>' if url else ""
    tab_names = ", ".join(
        t.get("name", "") if isinstance(t, dict) else str(t) for t in tabs
    ) or "None"
    return (
        f"<b>{title}</b>&nbsp;&nbsp;{link}<br>"
        f"<small>ID: <code>{sid}</code></small><br>"
        f"<b>Tabs:</b> {tab_names}<br>"
    )


def _fmt_sheet_data(data) -> str:
    if not isinstance(data, dict):
        return str(data)
    values = data.get("values", [])
    if not values:
        return "Range is empty."
    rows = ["<table style='border-collapse:collapse;font-size:0.9em;margin-top:6px'>"]
    for r_idx, row in enumerate(values):
        tag = "th" if r_idx == 0 else "td"
        cells = "".join(
            f"<{tag} style='border:1px solid #555;padding:4px 8px'>{cell}</{tag}>"
            for cell in row
        )
        rows.append(f"<tr>{cells}</tr>")
    rows.append("</table>")
    return "".join(rows)


def _fmt_file_list(files, label="files") -> str:
    if not isinstance(files, list) or not files:
        return f"No {label} found."
    lines = [f"<b>Found {len(files)} {label}:</b><br><br>"]
    for i, f in enumerate(files, 1):
        name      = f.get("name", "(Untitled)")
        url       = f.get("url", "")
        ftype     = f.get("type", "")
        modified  = f.get("modified", "")[:10]
        size      = f.get("size", "")
        fid       = f.get("id", "")
        link      = f'<a href="{url}" target="_blank">{name}</a>' if url else name
        size_str  = f"&nbsp;{size} bytes" if size else ""
        lines.append(
            f"<b>{i}.</b> {link} <small>({ftype})</small><br>"
            f"&nbsp;&nbsp;<small>Modified: {modified}{size_str}&nbsp;&nbsp;ID: <code>{fid}</code></small><br><br>"
        )
    return "".join(lines)


def _fmt_single_file(f) -> str:
    if not isinstance(f, dict):
        return str(f)
    name    = f.get("name", "(Untitled)")
    url     = f.get("url", "")
    ftype   = f.get("type", "")
    modified = f.get("modified", "")
    size    = f.get("size", "")
    fid     = f.get("id", "")
    owners  = f.get("owners", [])
    link    = f'<a href="{url}" target="_blank">Open ↗</a>' if url else ""
    return (
        f"<b>{name}</b>&nbsp;&nbsp;{link}<br>"
        f"<b>Type:</b> {ftype}<br>"
        f"<b>Modified:</b> {modified}<br>"
        f"{'<b>Size:</b> ' + str(size) + ' bytes<br>' if size else ''}"
        f"{'<b>Owners:</b> ' + ', '.join(owners) + '<br>' if owners else ''}"
        f"<small>ID: <code>{fid}</code></small>"
    )


def _fmt_storage(data) -> str:
    if not isinstance(data, dict):
        return str(data)
    used  = data.get("used_gb",    data.get("used", "?"))
    total = data.get("limit_gb",   data.get("limit", "?"))
    free  = data.get("free_gb",    data.get("free", "?"))
    pct   = data.get("percent_used", "?")
    return (
        f"<b>Google Drive Storage</b><br>"
        f"Used: <b>{used}</b><br>"
        f"Total: {total}<br>"
        f"Free: {free}<br>"
        f"Usage: {pct}"
    )


def _fmt_permissions(data) -> str:
    perms = data if isinstance(data, list) else data.get("permissions", []) if isinstance(data, dict) else []
    if not perms:
        return "No permissions found."
    lines = [f"<b>Permissions ({len(perms)}):</b><br>"]
    for p in perms:
        if isinstance(p, dict):
            email = p.get("emailAddress", p.get("email", ""))
            role  = p.get("role", "")
            lines.append(f"• {email} — <b>{role}</b><br>")
    return "".join(lines)


# ── Main formatter ───────────────────────────────────────────────────────────

def format_tool_result(tool_name: str, mcp_result: dict) -> str:
    """Format any MCP tool result as clean HTML."""
    if not isinstance(mcp_result, dict):
        return str(mcp_result)
    if not mcp_result.get("success"):
        err = mcp_result.get("error", "Unknown error")
        return f"<b style='color:#ef4444'>Error:</b> {err}"

    data = mcp_result.get("result", "")

    # ── Gmail ─────────────────────────────────────────────────────────────────
    if tool_name in ("get_emails", "get_unread_emails", "get_starred_emails",
                     "search_emails", "get_emails_by_sender", "get_emails_by_label",
                     "get_emails_by_date_range", "get_emails_v2", "search_emails_v2"):
        # Handle multiple response formats:
        # v1: list of emails
        # v1.5: {"success": True, "emails": [...], "count": N}
        # v2: {"items": [...], "next_page_token": ..., "has_more": ...}
        emails = []
        if isinstance(data, dict):
            # Check for error
            if not data.get("success", True) and "error" in data:
                error_msg = data.get("error", "Unknown error")
                return f"❌ Search failed: {error_msg}"
            # Try v1.5 format (emails key)
            if "emails" in data:
                emails = data.get("emails", [])
                count = data.get("count", len(emails))
                message = data.get("message", "")
                if count == 0 and message:
                    return f"📭 {message}"
            # Try v2 format (items key)
            elif "items" in data:
                emails = data.get("items", [])
                if not emails:
                    return "📭 No emails found"
            else:
                return f"❌ Unexpected response format: {str(data)[:200]}"
        elif isinstance(data, list):
            # Legacy v1 format
            emails = data
        
        return _fmt_email_list(emails)

    if tool_name == "get_email_by_id":
        return _fmt_single_email(data)

    if tool_name == "send_email":
        mid = data.get("id", "") if isinstance(data, dict) else ""
        return f"✅ Email sent!<br><small>Message ID: <code>{mid}</code></small>"

    if tool_name == "draft_email":
        did = data.get("id", "") if isinstance(data, dict) else ""
        return (
            f"✅ Draft saved!<br>"
            f"Draft ID: <code>{did}</code><br>"
            f"<small>Use this ID to send or edit the draft later.</small>"
        )

    if tool_name == "send_draft":
        mid = data.get("id", "") if isinstance(data, dict) else ""
        return f"✅ Draft sent!<br><small>Message ID: <code>{mid}</code></small>"

    if tool_name == "reply_email":
        return "✅ Reply sent."

    if tool_name == "reply_all":
        return "✅ Reply all sent."

    if tool_name == "forward_email":
        return "✅ Email forwarded."

    if tool_name == "trash_email":
        return "✅ Email moved to trash."

    if tool_name == "archive_email":
        return "✅ Email archived."

    if tool_name == "restore_email":
        return "✅ Email restored."

    if tool_name == "delete_email":
        return "✅ Email permanently deleted."

    if tool_name == "star_email":
        return "✅ Email starred."

    if tool_name == "unstar_email":
        return "✅ Email unstarred."

    if tool_name == "mark_as_read":
        return "✅ Marked as read."

    if tool_name == "mark_as_unread":
        return "✅ Marked as unread."

    if tool_name == "delete_draft":
        return "✅ Draft deleted."

    if tool_name == "list_labels":
        return _fmt_label_list(data)

    if tool_name in ("add_label",):
        return "✅ Label added."

    if tool_name in ("remove_label",):
        return "✅ Label removed."

    if tool_name == "create_label":
        return "✅ Label created."

    if tool_name == "get_email_thread":
        return _fmt_thread(data)

    if tool_name in ("summarize_email", "summarize_emails", "draft_reply",
                     "classify_email", "detect_urgency", "sentiment_analysis",
                     "extract_tasks", "extract_dates", "extract_contacts",
                     "extract_links", "generate_followup", "rewrite_email",
                     "translate_email"):
        return f"<p style='white-space:pre-wrap'>{data}</p>"

    # ── Docs ──────────────────────────────────────────────────────────────────
    if tool_name in ("list_docs", "search_docs"):
        return _fmt_doc_list(data)

    if tool_name in ("get_doc", "get_doc_content"):
        return _fmt_single_doc(data if isinstance(data, dict) else {"text": data})

    if tool_name == "create_doc":
        if isinstance(data, dict):
            title  = data.get("title", "")
            url    = data.get("url", "")
            doc_id = data.get("id", "")
            link   = f'<a href="{url}" target="_blank">{title}</a>' if url else title
            return f"✅ Document created: <b>{link}</b><br><small>ID: <code>{doc_id}</code></small>"
        return "✅ Document created."

    if tool_name == "append_to_doc":
        return "✅ Content added to document."

    if tool_name == "replace_text_in_doc":
        return "✅ Text replaced in document."

    if tool_name == "update_doc_title":
        return "✅ Document title updated."

    if tool_name == "delete_doc":
        return "✅ Document moved to trash."

    # ── Sheets ────────────────────────────────────────────────────────────────
    if tool_name in ("list_sheets", "search_sheets"):
        return _fmt_sheet_list(data)

    if tool_name == "get_sheet":
        return _fmt_single_sheet(data)

    if tool_name == "read_sheet":
        return _fmt_sheet_data(data)

    if tool_name == "create_sheet":
        if isinstance(data, dict):
            title = data.get("title", "")
            url   = data.get("url", "")
            sid   = data.get("id", "")
            link  = f'<a href="{url}" target="_blank">{title}</a>' if url else title
            return f"✅ Spreadsheet created: <b>{link}</b><br><small>ID: <code>{sid}</code></small>"
        return "✅ Spreadsheet created."

    if tool_name in ("write_to_sheet", "append_to_sheet"):
        return "✅ Data written to spreadsheet."

    if tool_name == "clear_sheet_range":
        return "✅ Range cleared."

    if tool_name == "add_sheet_tab":
        return "✅ Tab added."

    if tool_name == "rename_sheet_tab":
        return "✅ Tab renamed."

    if tool_name == "delete_sheet":
        return "✅ Spreadsheet moved to trash."

    # ── Drive ─────────────────────────────────────────────────────────────────
    if tool_name in ("list_files", "search_files", "search_files_by_type",
                     "get_starred_files", "get_recent_files", "get_folder_contents"):
        return _fmt_file_list(data)

    if tool_name == "list_folders":
        return _fmt_file_list(data, label="folders")

    if tool_name == "get_file_metadata":
        return _fmt_single_file(data)

    if tool_name == "get_storage_info":
        return _fmt_storage(data)

    if tool_name == "create_folder":
        if isinstance(data, dict):
            name = data.get("name", "")
            fid  = data.get("id", "")
            return f"✅ Folder <b>{name}</b> created.<br><small>ID: <code>{fid}</code></small>"
        return "✅ Folder created."

    if tool_name == "rename_file":
        name = data.get("name", "") if isinstance(data, dict) else ""
        return f"✅ Renamed to <b>{name}</b>." if name else "✅ File renamed."

    if tool_name == "move_file":
        return "✅ File moved."

    if tool_name == "copy_file":
        name = data.get("name", "") if isinstance(data, dict) else ""
        return f"✅ Copied as <b>{name}</b>." if name else "✅ File copied."

    if tool_name == "trash_file":
        return "✅ File moved to trash."

    if tool_name == "restore_file":
        return "✅ File restored from trash."

    if tool_name == "delete_file":
        return "✅ File permanently deleted."

    if tool_name == "trash_folder":
        return "✅ Folder moved to trash."

    if tool_name == "restore_folder":
        return "✅ Folder restored from trash."

    if tool_name == "delete_folder":
        return "✅ Folder permanently deleted."

    if tool_name == "share_file":
        return "✅ File shared."

    if tool_name == "share_file_publicly":
        link = data.get("link", data.get("url", "")) if isinstance(data, dict) else ""
        return (f"✅ File made public.<br><a href='{link}' target='_blank'>{link}</a>"
                if link else "✅ File made public.")

    if tool_name == "get_shareable_link":
        link = data.get("link", data.get("url", str(data))) if isinstance(data, dict) else str(data)
        return f"🔗 <a href='{link}' target='_blank'>{link}</a>"

    if tool_name == "get_file_permissions":
        return _fmt_permissions(data)

    if tool_name in ("remove_permission", "remove_access", "make_file_private"):
        return "✅ Permissions updated."

    if tool_name == "upload_file":
        if isinstance(data, dict):
            name = data.get("name", "")
            fid  = data.get("id", "")
            return f"✅ <b>{name}</b> uploaded.<br><small>ID: <code>{fid}</code></small>"
        return "✅ File uploaded."

    # Fallback
    return f"<p style='white-space:pre-wrap'>{data}</p>"


def _fmt_v2(res_data: dict) -> str:
    """Legacy alias — use format_tool_result when tool name is known."""
    return format_tool_result("", res_data)


# ============================================================================
# SECTION 5: SYSTEM PROMPTS + LLM RESOLUTION
# ============================================================================

SYSTEM_PROMPT_GMAIL = """You are G-Assistant, an intelligent Gmail assistant.

CRITICAL: Respond ONLY with valid JSON in one of the two formats below.
ALWAYS use the Keyword → Tool Mapping table below. NEVER default to get_emails when the user asks about starred, unread, or specific date ranges.

## Tool call format
```json
{
  "requires_tool": true,
  "tool": "tool_name",
  "arguments": {"arg1": "value1"},
  "explanation": "Brief description of what you are doing"
}
```

## Direct response format (no tool needed)
```json
{
  "requires_tool": false,
  "response": "Your natural language response here"
}
```

## Compound commands
If the user asks for two things in one sentence (e.g. "show unread and mark first as read"), return ONLY the first tool call. The system will execute it, update context, and then call you again for the second step automatically.

## Available Gmail Tools

### Reading
- get_emails(limit) — list recent emails
- get_email_by_id(message_id) — read a specific email
- get_unread_emails(limit) — list unread emails
- get_starred_emails(limit) — list starred emails
- search_emails(query) — search by keyword, sender, subject
- get_emails_by_sender(sender) — emails from a specific sender
- get_emails_by_label(label) — emails with a label
- get_emails_by_date_range(start, end) — emails in a date range (format: YYYY/MM/DD)
- get_email_thread(thread_id) — get a full conversation thread
- get_attachments(message_id) — list attachments in an email

## Keyword → Tool Mapping (MANDATORY)
Match the user's words to the correct tool. NEVER guess.

| User says | Tool to use |
|-----------|-------------|
| "starred", "starred emails", "starred messages", "favorites" | get_starred_emails |
| "unread", "new emails", "haven't read", "any new" | get_unread_emails |
| "inbox", "recent emails", "my emails", "what's in my inbox" | get_emails |
| "labels", "categories", "my labels" | list_labels |
| "from [sender]" | search_emails with query "from:[sender]" |
| "yesterday", "today", "last week" | get_emails_by_date_range |

### Sending & Drafts

CRITICAL DISTINCTION — read carefully:
- draft_email(to, subject, body) — SAVE as draft, does NOT send. Use when user says "draft", "create a draft", "save as draft", "write a draft".
- send_email(to, subject, body) — SENDS immediately. Use only when user says "send email", "mail this to", "shoot an email".
If the user says the word "draft" anywhere, ALWAYS use draft_email, never send_email.
- send_draft(draft_id) — send a saved draft
- update_draft(draft_id, to, subject, body) — edit a draft
- delete_draft(draft_id) — delete a draft
- reply_email(message_id, body) — reply to an email
- reply_all(message_id, body) — reply to all
- forward_email(message_id, to) — forward an email

### Organisation
- list_labels() — list all labels
- add_label(message_id, label) — label an email
- remove_label(message_id, label) — remove a label
- create_label(label_name) — create a new label
- mark_as_read(message_id) — mark as read
- mark_as_unread(message_id) — mark as unread
- star_email(message_id) — star an email
- unstar_email(message_id) — unstar
- archive_email(message_id) — archive
- trash_email(message_id) — move to trash (recoverable). Use this for "delete", "remove", "get rid of" commands.
- restore_email(message_id) — restore from trash
- delete_email(message_id) — permanently delete (NOT recoverable). Only use if user explicitly says "permanently delete" or "delete forever"

### AI Tools
- summarize_email(message_id) — summarise an email
- summarize_emails(limit) — summarise recent emails
- extract_tasks(message_id) — extract action items
- detect_urgency(message_id) — check if urgent
- draft_reply(message_id, tone) — draft a reply suggestion
- sentiment_analysis(message_id) — analyse tone/sentiment

## Context Resolution

You MUST resolve pronouns and positional references using the CURRENT CONTEXT above. NEVER ask the user for an ID when context is available.

### Positional mapping rules
- "1st email", "first one", "this", "it", "the last email", "most recent" → use the 1st ID in the context list.
- "2nd email", "second one", "that" → use the 2nd ID in the context list.
- "3rd email" / "third one" → use the 3rd ID.
- "4th" → 4th ID, "5th" → 5th ID.
- "the last email" also means the most recent / 1st ID.

### Pronoun rules
- "this", "it", "that" after an email was listed → resolve to the current email ID shown in context.
- "that" when two emails were discussed → usually means the 2nd one.
- If there is a "Current email" in context, use its ID for any pronoun.

Examples:
1. Context shows 1st=ID:19d95f... → User: "delete this" → {"tool": "trash_email", "arguments": {"message_id": "19d95f..."}}
2. Context shows draft ID:r123 → User: "send it" → {"tool": "send_draft", "arguments": {"draft_id": "r123"}}
3. Context shows 3 emails, 2nd=ID:abc... → User: "reply to the second one saying sure" → {"tool": "reply_email", "arguments": {"message_id": "abc...", "body": "sure"}}
4. Context shows 1st=ID:x123... → User: "summarize the 1st email" → {"tool": "summarize_email", "arguments": {"message_id": "x123..."}}
5. Context shows 1st=ID:y456... → User: "draft a reply to the last email in formal tone" → {"tool": "draft_reply", "arguments": {"message_id": "y456...", "tone": "formal"}}

## If uncertain
```json
{"requires_tool": false, "response": "I'm not sure which email you mean. Could you clarify or provide the ID?"}
```

Always scan conversation history for IDs before asking for clarification."""


SYSTEM_PROMPT_DOCS = """You are G-Assistant, an intelligent Google Docs assistant.

CRITICAL: Respond ONLY with valid JSON in one of the two formats below.

## Tool call format
```json
{
  "requires_tool": true,
  "tool": "tool_name",
  "arguments": {"arg1": "value1"},
  "explanation": "Brief description"
}
```

## Direct response format
```json
{"requires_tool": false, "response": "Your response here"}
```

## Available Docs Tools

- list_docs(limit) — list recent documents
- search_docs(query) — search documents by name or content
- get_doc(doc_id) — view a document's title and content
- get_doc_content(doc_id) — get plain-text body only
- create_doc(title, content) — create a new document
- append_to_doc(doc_id, text) — add text to the end
- replace_text_in_doc(doc_id, find, replace) — find and replace text
- update_doc_title(doc_id, new_title) — rename a document
- delete_doc(doc_id) — move document to trash

## Context Resolution

When the user says "this", "that", "it", look at conversation history for the doc_id.

Examples:
1. History: "Document 'Project Proposal' created (ID: 1BxiMV...)"
   User: "add some content to it"
   → {"requires_tool": true, "tool": "append_to_doc", "arguments": {"doc_id": "1BxiMV...", "text": "..."}}

2. History: "Documents: 1. Meeting Notes (ID: 1abc...) 2. Budget (ID: 2def...)"
   User: "open the second one"
   → {"requires_tool": true, "tool": "get_doc", "arguments": {"doc_id": "2def..."}}

## If uncertain
```json
{"requires_tool": false, "response": "Which document do you mean? Please clarify or provide the document ID."}
```"""


SYSTEM_PROMPT_SHEETS = """You are G-Assistant, an intelligent Google Sheets assistant.

CRITICAL: Respond ONLY with valid JSON in one of the two formats below.

## Tool call format
```json
{
  "requires_tool": true,
  "tool": "tool_name",
  "arguments": {"arg1": "value1"},
  "explanation": "Brief description"
}
```

## Direct response format
```json
{"requires_tool": false, "response": "Your response here"}
```

## Available Sheets Tools

- list_sheets(limit) — list recent spreadsheets
- search_sheets(query) — search spreadsheets by name
- get_sheet(sheet_id) — view spreadsheet metadata and tab names
- read_sheet(sheet_id, range_name) — read cell values (e.g. range_name="A1:C10")
- create_sheet(title) — create a new spreadsheet
- write_to_sheet(sheet_id, range_name, values) — write data to a range
- append_to_sheet(sheet_id, range_name, values) — append row(s) (use range_name="A1" for auto-append)
- clear_sheet_range(sheet_id, range_name) — clear a cell range
- add_sheet_tab(sheet_id, tab_name) — add a new tab/worksheet
- rename_sheet_tab(sheet_id, old_name, new_name) — rename a tab
- delete_sheet(sheet_id) — move spreadsheet to trash

## Context Resolution

When the user says "this", "that", "it", look at conversation history for the sheet_id.

Examples:
1. History: "Spreadsheet 'Budget 2024' created (ID: 1abc...)"
   User: "add a new row with Q1 data"
   → {"requires_tool": true, "tool": "append_to_sheet", "arguments": {"sheet_id": "1abc...", "range_name": "A1", "values": [["Q1", "..."]]} }

2. History: "Sheet 'Sales' (ID: 2def...)" → User: "what's in cell B5"
   → {"requires_tool": true, "tool": "read_sheet", "arguments": {"sheet_id": "2def...", "range_name": "B5"}}

## If uncertain
```json
{"requires_tool": false, "response": "Which spreadsheet do you mean? Please clarify or provide the sheet ID."}
```"""


SYSTEM_PROMPT_DRIVE = """You are G-Assistant, an intelligent Google Drive assistant.

CRITICAL: Respond ONLY with valid JSON in one of the two formats below.

## Tool call format
```json
{
  "requires_tool": true,
  "tool": "tool_name",
  "arguments": {"arg1": "value1"},
  "explanation": "Brief description"
}
```

## Direct response format
```json
{"requires_tool": false, "response": "Your response here"}
```

## Available Drive Tools

### Browse
- list_files(limit) — list recent files
- list_folders(limit) — list folders
- get_folder_contents(folder_id, limit) — list files inside a folder
- get_starred_files(limit) — list starred files
- get_recent_files(limit) — list recently modified files

### Search
- search_files(query, limit) — search by name or content
- search_files_by_type(file_type, limit) — search by type (e.g. "doc", "pdf", "sheet")

### Metadata
- get_file_metadata(file_id) — get full file details
- get_storage_info() — check Drive storage usage

### Create & Organise
- create_folder(name, parent_id) — create a new folder (parent_id optional)
- rename_file(file_id, new_name) — rename a file or folder
- move_file(file_id, destination_folder_id) — move file to a folder
- copy_file(file_id, new_name) — copy a file

### Trash / Delete (Files)
- trash_file(file_id) — move file to trash
- restore_file(file_id) — restore file from trash
- delete_file(file_id) — permanently delete file

### Trash / Delete (Folders)
- trash_folder(folder_id) — move folder to trash
- restore_folder(folder_id) — restore folder from trash
- delete_folder(folder_id) — permanently delete folder

### Sharing
- share_file(file_id, email, role) — share with a user (role: reader/writer/commenter)
- share_file_publicly(file_id) — make file publicly viewable
- get_shareable_link(file_id) — get a shareable link
- get_file_permissions(file_id) — list who has access
- remove_access(file_id, email) — remove someone's access
- make_file_private(file_id) — remove all sharing

### Upload
- upload_file(file_path, folder_id) — upload a local file to Drive (folder_id optional)

## Context Resolution

When the user says "this", "that", "it", look at conversation history for the file_id or folder_id.

Examples:
1. History: "File 'Report.pdf' (ID: 1abc...)" → User: "share it with john@example.com"
   → {"requires_tool": true, "tool": "share_file", "arguments": {"file_id": "1abc...", "email": "john@example.com", "role": "reader"}}

2. History: list of files → User: "move the first one to Projects folder"
   → Resolve file_id from first item, search for Projects folder if needed

## If uncertain
```json
{"requires_tool": false, "response": "Which file do you mean? Please clarify or provide the file ID."}
```"""


def get_system_prompt(mode: str) -> str:
    prompts = {
        "gmail":  SYSTEM_PROMPT_GMAIL,
        "docs":   SYSTEM_PROMPT_DOCS,
        "sheets": SYSTEM_PROMPT_SHEETS,
        "drive":  SYSTEM_PROMPT_DRIVE,
    }
    return prompts.get(mode, SYSTEM_PROMPT_GMAIL)


def _sanitize_llm_output(text: str) -> str:
    """Strip tool call tokens, markdown fences, and JSON debris so raw LLM output never leaks to user."""
    # Remove LM Studio tokens
    text = re.sub(r'<\|tool_call\>(?:call:\s*)?', '', text)
    text = re.sub(r'<tool_call\|>', '', text)
    # Remove markdown fences
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    # Remove stray braces/parentheses that are just syntax debris
    text = text.replace('{}', '').replace('{ }', '')
    # Clean up extra whitespace
    text = text.strip()
    return text


def _html_to_plain(html: str) -> str:
    """Strip HTML tags and decode entities for clean LLM history entries."""
    # Remove all HTML tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Decode common HTML entities
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&')
    text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&apos;', "'")
    # Collapse excess whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()[:1500]  # Cap so history stays compact


def parse_llm_response(response_text: str) -> dict:
    """Parse structured JSON from LLM response.

    Handles:
    - Plain JSON
    - ```json ... ``` fences
    - Bare tool names like get_emails() or get_emails(limit=10)
    - <|tool_call>call:{...} tokens (LM Studio / Mistral models)
    - JSON buried inside prose (last-resort extraction)
    """
    original = response_text
    
    # Strip markdown fences first so bare tools inside fences can be matched!
    if "```json" in response_text:
        m = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
        if m:
            response_text = m.group(1)
    elif "```" in response_text:
        m = re.search(r'```([a-zA-Z0-9_]*)\s*(.*?)\s*```', response_text, re.DOTALL)
        if m:
            response_text = m.group(2)
            
    try:
        stripped = response_text.strip()
        
        # Clean up stray empty braces at the end like get_emails(){}
        stripped = re.sub(r'\{\s*\}$', '', stripped).strip()
        
        # Handle bare tool name: "get_emails()" or "get_emails" or "tool_name(args...)"
        if stripped and not stripped.startswith('{') and not stripped.startswith('['):
            bare_tool_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\(\s*\))?$', stripped)
            if bare_tool_match:
                tool_name = bare_tool_match.group(1)
                return {
                    "requires_tool": True,
                    "tool": tool_name,
                    "arguments": {},
                    "explanation": f"Calling {tool_name}"
                }
            
            kwargs_match = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*\(\s*(.*?)\s*\)$', stripped, re.DOTALL)
            if kwargs_match:
                tool_name = kwargs_match.group(1)
                args_text = kwargs_match.group(2).strip()
                args = {}
                if args_text:
                    for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', args_text):
                        args[m.group(1)] = m.group(2)
                    for m in re.finditer(r"(\w+)\s*=\s*'([^']*)'", args_text):
                        args[m.group(1)] = m.group(2)
                    for m in re.finditer(r'(\w+)\s*=\s*(\d+)', args_text):
                        args[m.group(1)] = int(m.group(2))
                    for m in re.finditer(r'(\w+)\s*=\s*([^,\s]+)', args_text):
                        if m.group(1) not in args:
                            args[m.group(1)] = m.group(2)
                return {
                    "requires_tool": True,
                    "tool": tool_name,
                    "arguments": args,
                    "explanation": f"Calling {tool_name}"
                }

        # LM Studio proprietary format handling...
        if "<|tool_call>" in response_text:
            lms_match = re.search(r'<\|tool_call\>call:(\w+)\{(.*?)\}(?:<tool_call\|>)?', response_text, re.DOTALL)
            if lms_match:
                tool_name = lms_match.group(1)
                args_raw = lms_match.group(2)
                pairs = re.findall(r'(\w+):<\|\"\|>(.*?)<\|\"\|>', args_raw, re.DOTALL)
                if pairs:
                    return {"requires_tool": True, "tool": tool_name, "arguments": {k: v for k, v in pairs}}
                args_raw = args_raw.replace('<|"|>', '"')
                args_raw = re.sub(r'(?<!["\w])(\w+)\s*:', r'"\1":', args_raw)
                try:
                    return {"requires_tool": True, "tool": tool_name, "arguments": json.loads('{' + args_raw + '}')}
                except json.JSONDecodeError:
                    pass

            paren_match = re.search(r'<\|tool_call\>call:(\w+)\s*\(([^)]*)\)', response_text, re.DOTALL)
            if paren_match:
                tool_name = paren_match.group(1)
                inner = paren_match.group(2).strip()
                args = {}
                if inner:
                    if inner.startswith('{'):
                        try:
                            args = json.loads(inner)
                        except json.JSONDecodeError:
                            pass
                    else:
                        for m in re.finditer(r'(\w+)\s*=\s*"([^"]*)"', inner):
                            args[m.group(1)] = m.group(2)
                        for m in re.finditer(r"(\w+)\s*=\s*'([^']*)'", inner):
                            args[m.group(1)] = m.group(2)
                        for m in re.finditer(r'(\w+)\s*=\s*([^,\s]+)', inner):
                            k = m.group(1)
                            if k not in args:
                                args[k] = m.group(2)
                return {"requires_tool": True, "tool": tool_name, "arguments": args}

        if "<|tool_call>" in response_text and ':' in response_text:
            comma_pattern = r'<\|tool_call\>call:\s*"([^"]+)"\s*,\s*(.*)'
            match = re.search(comma_pattern, response_text, re.DOTALL)
            if match:
                tool_name = match.group(1)
                args_text = match.group(2).strip()
                try:
                    args_list = json.loads(f"[{args_text}]")
                    args = {}
                    if len(args_list) >= 1 and isinstance(args_list[0], str):
                        if len(args_list[0]) > 20: args["doc_id"] = args_list[0]
                        else: args["find"] = args_list[0]
                    if len(args_list) >= 2 and isinstance(args_list[1], str):
                        if "replace" in tool_name.lower(): args["replace"] = args_list[1]
                        else: args["content"] = args_list[1]
                    return {"requires_tool": True, "tool": tool_name, "arguments": args}
                except json.JSONDecodeError:
                    pass

        if "<|tool_call>" in response_text:
            response_text = re.sub(r'<\|tool_call\>(?:call:)?', '', response_text).strip()
            
        response_text = re.sub(r'<tool_call\|>', '', response_text).strip()

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            m = re.search(r'\{.*\}', response_text, re.DOTALL)
            if m:
                data = json.loads(m.group(0))
            else:
                clean = _sanitize_llm_output(original)
                return {"requires_tool": False, "response": clean}

        # Fix where requires_tool might be omitted but tool is present
        if "requires_tool" not in data:
            if "tool" in data or "tool_name" in data or "name" in data:
                data["requires_tool"] = True
            else:
                clean = _sanitize_llm_output(original)
                return {"requires_tool": False, "response": clean}

        rt = data.get("requires_tool")

        if isinstance(rt, list):
            valid_calls = [tc for tc in rt if isinstance(tc, dict) and tc.get("tool")]
            if valid_calls:
                first = valid_calls[0]
                data["tool"] = first.get("tool", "")
                data["arguments"] = first.get("arguments", {})
                data["requires_tool"] = True
                if len(valid_calls) > 1:
                    data["tool_calls"] = valid_calls
            else:
                data["requires_tool"] = bool(rt)
        elif isinstance(rt, str):
            rt_lower = rt.lower()
            if rt_lower not in ("false", "no", "0", "", "true", "yes", "1"):
                data["tool"] = rt
                data["requires_tool"] = True
            else:
                data["requires_tool"] = rt_lower not in ("false", "no", "0", "")
        
        if "tool" not in data and "requires_tool" in data:
            if "tool_name" in data: data["tool"] = data["tool_name"]
            elif "name" in data: data["tool"] = data["name"]
        
        if "arguments" not in data:
            args = {}
            for key in ["doc_id", "title", "content", "find", "replace", "limit", "query", "reference"]:
                if key in data:
                    args[key] = data.pop(key)
            data["arguments"] = args

        return data

    except Exception:
        clean = _sanitize_llm_output(original)
        return {"requires_tool": False, "response": clean}


def build_messages_with_context(
    user_input: str,
    mode: str,
    ctx: ContextState
) -> list:
    """Build the message list sent to the LLM: system prompt + context + history + user input."""
    messages = [{"role": "system", "content": get_system_prompt(mode)}]

    # Inject current entity summary so LLM can resolve pronouns
    context_lines = []
    for entity_type, entity in ctx.entities.items():
        if entity:
            line = f"- Current {entity_type}: ID={entity.id}"
            attrs = entity.attributes
            label = attrs.get("subject") or attrs.get("title") or attrs.get("name") or ""
            if label:
                line += f" ({label})"
            context_lines.append(line)
    if ctx.last_action:
        # Strip internal suffixes like _retrieved, _done, _failed, _viewed etc.
        # to avoid confusing the LLM into using them as tool names.
        clean_action = re.sub(
            r'_(retrieved|done|failed|viewed|sent|created|deleted|listed|updated|trashed)$',
            '', ctx.last_action
        )
        context_lines.append(f"- Last completed tool: {clean_action}")

    # Always inject current date so LLM can correctly resolve relative dates
    # ("last week", "yesterday", "today", etc.)
    today = datetime.date.today()
    context_lines.append(
        f"- Today's date: {today.strftime('%Y-%m-%d')} ({today.strftime('%A, %d %B %Y')})"
    )

    # Gmail convenience context
    if mode == "gmail":
        if ctx.last_draft_id:
            context_lines.append(f"- Last saved draft ID: {ctx.last_draft_id}")
        if ctx.last_to:
            subj_hint = f" (subject: {ctx.last_subject})" if ctx.last_subject else ""
            context_lines.append(f"- Last email/draft was to: {ctx.last_to}{subj_hint}")
        if ctx.last_email_list:
            for idx, em in enumerate(ctx.last_email_list[:5], start=1):
                subj = em.get("subject", "no subject")[:50]
                frm = em.get("from", "unknown")[:30]
                pronouns = {
                    1: "1st / this / it / last / most recent",
                    2: "2nd / that",
                    3: "3rd",
                    4: "4th",
                    5: "5th",
                }.get(idx, f"{idx}th")
                context_lines.append(
                    f"- {pronouns}: ID={em['id']} | From: {frm} | Subject: {subj}"
                )

    if context_lines:
        messages.append({
            "role": "system",
            "content": "Current context:\n" + "\n".join(context_lines)
        })

    # Conversation history for this mode
    history_key = f"history_{mode}"
    if history_key in ctx.history:
        messages.extend(ctx.history[history_key][-10:])

    messages.append({"role": "user", "content": user_input})
    return messages


def _heal_tool_name(tool_name: str, available: dict) -> str:
    """
    Auto-heal hallucinated tool names from the LLM.
    Strips common suffixes (_retrieved, _done, _failed, etc.)
    and returns the corrected name if it exists in available tools.
    Returns original name if no healing is possible.
    """
    if tool_name in available:
        return tool_name
    _SUFFIXES = [
        '_retrieved', '_done', '_failed', '_viewed', '_sent', '_created',
        '_deleted', '_listed', '_updated', '_trashed', '_archived', '_found',
    ]
    for suffix in _SUFFIXES:
        if tool_name.endswith(suffix):
            candidate = tool_name[:-len(suffix)]
            if candidate in available:
                logger.info("[HEAL] Auto-healed tool name: %s → %s", tool_name, candidate)
                return candidate
    return tool_name


def _call_llm_single_step(user_input: str, mcp, ctx: ContextState, mode: str,
                          extra_system: str = "") -> tuple:
    """One LLM call → execute one tool → return (formatted_response, tool_result_dict, error)."""
    messages = build_messages_with_context(user_input, mode, ctx)
    if extra_system:
        messages.append({"role": "system", "content": extra_system})

    response_data = call_model(messages)
    try:
        response_text = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error("Unexpected LLM response shape: %s", response_data)
        return (f"❌ LLM response error: {e}", None, None)

    logger.debug("[LLM RAW] %s", response_text[:500])

    parsed = parse_llm_response(response_text)
    logger.debug("[LLM PARSED] requires_tool=%s tool=%s args=%s",
                 parsed.get('requires_tool'), parsed.get('tool'), parsed.get('arguments'))

    # Resolve available tools early — needed for validation in the buried-tool check below
    available = get_available_tools(mode)

    if not parsed.get("requires_tool", False):
        response_val = parsed.get("response", response_text)
        cleaned_val = response_val.strip()

        # Case 1: response field contains an explicit <|tool_call> token or function-call syntax.
        _has_buried_call = (
            "<|tool_call>" in response_val or
            bool(re.match(r'^\s*[a-zA-Z_]\w*\s*\(', cleaned_val))
        )

        # Case 2: response field is a bare identifier that exactly matches (or heals to) a known tool.
        _bare_as_tool = False
        _bare_name = ""
        if not _has_buried_call and re.match(r'^[a-zA-Z_]\w*$', cleaned_val):
            healed = _heal_tool_name(cleaned_val, available)
            if healed in available:
                _bare_as_tool = True
                _bare_name = healed

        if _has_buried_call:
            logger.debug("[LLM] Buried call in response field, re-parsing: %s", response_val[:200])
            reparsed = parse_llm_response(response_val)
            if reparsed.get("requires_tool") and reparsed.get("tool"):
                parsed = reparsed
                logger.debug("[LLM REPARSED] tool=%s args=%s", parsed.get('tool'), parsed.get('arguments'))
            else:
                return (_sanitize_llm_output(response_val), None, None)
        elif _bare_as_tool:
            logger.debug("[LLM] Response field is valid bare tool name, treating as call: %s", _bare_name)
            parsed = {"requires_tool": True, "tool": _bare_name, "arguments": {}}
        else:
            return (_sanitize_llm_output(response_val), None, None)

    # Handle batch tool calls
    tool_calls = parsed.get("tool_calls", [])
    if tool_calls:
        responses = []
        for tc in tool_calls:
            tc_name = _heal_tool_name(tc.get("tool", ""), available)
            tc_args = tc.get("arguments", {})
            if not tc_name or tc_name not in available:
                responses.append(f"⚠️ Unknown tool: <code>{tc_name}</code>")
                continue
            tc_result, tc_error = execute_tool_safe(mcp, tc_name, tc_args, ctx)
            update_context_from_tool_result(ctx, tc_name, tc_args, tc_result)
            if tc_error == "needs_clarification":
                responses.append(f"⚠️ {tc_result.get('error', 'More information needed.')}")
            elif tc_result.get("success"):
                responses.append(format_tool_result(tc_name, tc_result))
            else:
                responses.append(f"❌ {tc_name} failed: {tc_result.get('error', 'Unknown error')}")
        return ("<br><br>".join(responses), None, None)

    tool_name = _heal_tool_name(parsed.get("tool", ""), available)
    arguments = parsed.get("arguments", {})
    explanation = parsed.get("explanation", "")

    if not tool_name:
        logger.warning("LLM returned requires_tool=true but empty tool name. Raw: %s",
                       response_text[:200])
        return ("I understood what you want but couldn't determine the right action. "
                "Could you rephrase?", None, None)
    if tool_name not in available:
        logger.warning("[LLM] Unknown tool after healing: %s", tool_name)
        return (f"<b style='color:#ef4444'>I couldn't identify the right action for that request.</b><br>"
                f"<small>Tool attempted: <code>{tool_name}</code></small>", None, None)

    result, error = execute_tool_safe(mcp, tool_name, arguments, ctx)
    update_context_from_tool_result(ctx, tool_name, arguments, result)

    if error == "needs_clarification":
        return (f"⚠️ {result.get('error', 'More information needed.')}", result, error)

    if result.get("success"):
        response = format_tool_result(tool_name, result)
        if explanation:
            response = f"✅ {explanation}<br><br>{response}"
        return (response, result, None)

    return (f"❌ Failed: {result.get('error', 'Unknown error')}", result, error)


def llm_resolve_and_execute(
    user_input: str,
    mcp,
    ctx: ContextState,
    mode: str
) -> str:
    """Send message to LLM, parse JSON response, execute tool if needed.

    For compound commands (e.g. "show unread and mark first as read") this
    splits the sentence into two LLM turns with context updates between them.
    """
    low = user_input.lower()
    compound_split = re.split(r'\s+(?:and|then)\s+', low, maxsplit=1)
    is_compound = (
        len(compound_split) == 2 and
        re.search(r'\b(?:show|list|get|check|fetch|give me)\b', compound_split[0]) and
        re.search(r'\b(?:mark|star|unstar|archive|unarchive|trash|delete|reply|forward|move|label|add|remove)\b', compound_split[1])
    )

    if not is_compound:
        return _call_llm_single_step(user_input, mcp, ctx, mode)[0]

    # ── COMPOUND: two LLM passes with context refresh ─────────────────────────
    first_half = user_input[:user_input.lower().find(compound_split[1])].strip()
    second_half = compound_split[1]
    if first_half.endswith((" and", " then")):
        first_half = first_half[:-4].strip()

    # Step 1: execute first half (typically a list command)
    resp1, res1, err1 = _call_llm_single_step(first_half, mcp, ctx, mode)

    if err1 == "needs_clarification":
        return resp1
    if res1 is None:
        # LLM returned prose instead of a tool call
        return resp1

    # Build a concise summary of what was found for the second LLM call
    summary = resp1
    if res1.get("emails"):
        emails = res1.get("emails", [])
        lines = ["Step 1 completed. Here are the results (use these IDs for step 2):"]
        for i, e in enumerate(emails[:5], start=1):
            eid = e.get("id", "")
            subj = e.get("subject", e.get("snippet", "no subject"))[:50]
            frm = e.get("from", "unknown")[:30]
            lines.append(f"  {i}. ID={eid} | From: {frm} | Subject: {subj}")
        summary = "\n".join(lines)
    elif res1.get("success"):
        summary = f"Step 1 completed. Result: {resp1[:300]}"

    # Step 2: execute second half with updated context
    resp2, _, _ = _call_llm_single_step(
        second_half, mcp, ctx, mode,
        extra_system=f"Previous step done. Use the current context below to resolve pronouns/positions.\n{summary}"
    )

    return f"{resp1}<br><br>{resp2}"


def get_available_tools(mode: str) -> dict:
    """Get available tools for a mode. Supports unified mode with all tools."""
    all_tools = {**GMAIL_TOOLS, **DOCS_TOOLS, **SHEETS_TOOLS, **DRIVE_TOOLS}
    
    mode_tools = {
        "gmail":  {**GMAIL_TOOLS, **DRIVE_TOOLS},  # Gmail + Drive for file attachments
        "docs":   {**DOCS_TOOLS, **DRIVE_TOOLS},   # Docs + Drive for management
        "sheets": {**SHEETS_TOOLS, **DRIVE_TOOLS}, # Sheets + Drive for management
        "drive":  DRIVE_TOOLS,
        "unified": all_tools,  # All 100+ tools available
        "all": all_tools,
    }
    
    return mode_tools.get(mode, all_tools)  # Default to all tools if mode not found


# ============================================================================
# SECTION 6: MAIN ORCHESTRATOR FLOW
# ============================================================================

def run_agent_v2(
    user_input: str,
    mcp,
    state: dict,
    mode: str = "gmail",
    _attachment: Any = None
) -> str:
    """
    Main entry point.

    Flow:
    1. Init context for mode
    2. Handle any pending multi-turn action
    3. Strict intent detection (fast, no LLM)
    4. LLM resolution (pronouns, complex queries)
    5. Update history, trim to 20 turns
    """
    ctx = get_or_create_context(state)
    ctx.active_mode = mode

    # Inject attachment from web layer so tools can use it
    attachment_path = state.get("last_attachment_path", "")
    if attachment_path:
        ctx.pending_attachment_path = attachment_path

    history_key = f"history_{mode}"
    if history_key not in ctx.history:
        ctx.history[history_key] = []

    # Pending multi-turn action
    if ctx.pending_action:
        pending_result = handle_pending_action(user_input, mcp, ctx, mode)
        if pending_result:
            ctx.history[history_key].append({"role": "user", "content": user_input})
            ctx.history[history_key].append({"role": "assistant", "content": pending_result})
            return pending_result

    # Strict intent (no LLM)
    intent_result = intent_detect_strict(user_input, mcp, state)
    if intent_result is not None:
        ctx.history[history_key].append({"role": "user", "content": user_input})
        # Store plain-text summary, not raw HTML, so the LLM doesn't see markup
        ctx.history[history_key].append({"role": "assistant", "content": _html_to_plain(intent_result)})
        ctx.history[history_key] = _trim_history_smart(ctx.history[history_key])
        if ctx.pending_attachment_path:
            ctx.pending_attachment_path = ""
            state.pop("last_attachment_path", None)
        return intent_result

    # LLM resolution
    llm_result = llm_resolve_and_execute(user_input, mcp, ctx, mode)

    ctx.history[history_key].append({"role": "user", "content": user_input})
    # Store plain-text summary in history (not HTML) so the LLM isn't confused by markup
    ctx.history[history_key].append({"role": "assistant", "content": _html_to_plain(llm_result)})

    ctx.history[history_key] = _trim_history_smart(ctx.history[history_key])

    # Clear consumed attachment so it isn't re-injected on next turn
    if ctx.pending_attachment_path:
        ctx.pending_attachment_path = ""
        state.pop("last_attachment_path", None)

    return llm_result


def handle_pending_action(
    user_input: str,
    mcp,
    ctx: ContextState,
    _mode: str
) -> Optional[str]:
    """Resume a multi-turn action by collecting one missing field at a time."""
    pending = ctx.pending_action
    if not pending:
        return None

    tool_name = pending.get("tool", "")
    collected = pending.get("collected", {})
    missing = pending.get("missing", [])

    if missing:
        field = missing[0]

        if field == "to":
            match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', user_input)
            if match:
                collected[field] = match.group(0)
                missing.pop(0)
        elif field in ("subject", "body", "text", "content", "title", "name"):
            collected[field] = user_input.strip()
            missing.pop(0)
        elif field == "draft_id":
            match = re.search(r'r\d+', user_input)
            if match:
                collected[field] = match.group(0)
                missing.pop(0)
        elif field == "message_id":
            match = re.search(r'[a-fA-F0-9]{10,}', user_input)
            if match:
                collected[field] = match.group(0)
                missing.pop(0)

    if missing:
        ctx.pending_action = {"tool": tool_name, "collected": collected, "missing": missing}
        return f"Please provide {missing[0]}:"

    ctx.pending_action = None
    result, _ = execute_tool_safe(mcp, tool_name, collected, ctx)
    return format_tool_result(tool_name, result)


# ============================================================================
# SECTION 7: BACKWARD COMPATIBILITY
# ============================================================================

def run_agent(user_input: str, mcp, state: dict, mode: str = "gmail") -> str:
    """Backward-compatible wrapper."""
    return run_agent_v2(user_input, mcp, state, mode)
