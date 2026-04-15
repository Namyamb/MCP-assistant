"""
orchestrator.py — Production-level agent orchestration for G-Assistant.

Routing table
─────────────
  gmail          → Python intent router (zero-latency fast path) ─► LLM with Gmail tools
  general        → LLM only, no tools
  drive/docs/sheets → "coming soon" gate (bypassed when a file is attached)

Attachment handling
───────────────────
  Documents (.docx, .txt, …) – text extracted by web.py, injected as inline context.
  Images (.png, .jpg, …)     – base64 sent as vision content block on the first turn only;
                               replaced with a slim text note in history to avoid context bloat.
"""

from __future__ import annotations

import json
import logging
import re
import datetime
from typing import Optional

from app.core.config import MAX_TOOL_LOOPS, MAX_HISTORY_MSG_CHARS

# ─── top-level imports (not inside functions) ───────────────────────────────
from app.core.llm_client import call_model                         # noqa: E402
from app.integrations.gmail.registry import GMAIL_TOOLS           # noqa: E402
from app.integrations.docs.registry import DOCS_TOOLS             # noqa: E402
from app.integrations.sheets.registry import SHEETS_TOOLS         # noqa: E402

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# HTML formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt(res_data: dict) -> str:
    """Recursively render MCP tool results as HTML."""
    if not isinstance(res_data, dict) or "success" not in res_data:
        return str(res_data)
    if not res_data.get("success"):
        err = res_data.get("error", "Unknown error")
        return f"<b style='color:#ef4444'>Error:</b> {err}"

    res = res_data.get("result", "")

    def _recurse(data) -> str:
        if isinstance(data, list):
            if not data:
                return "No results found."
            return "".join(
                f"<div style='padding:10px;margin:6px 0;background:rgba(99,102,241,.1);"
                f"border-left:3px solid var(--primary);border-radius:4px'>{_recurse(i)}</div>"
                for i in data
            )
        if isinstance(data, dict):
            if "subject" in data and "from" in data:
                sender = data.get("from", "").split("<")[0].strip()
                return (
                    f"<b>From:</b> {sender}<br>"
                    f"<b>Subject:</b> {data.get('subject','(No Subject)')}<br>"
                    f"<span style='color:var(--text-muted);font-size:13px'>{data.get('snippet','')}</span><br>"
                    f"<span style='color:var(--text-muted);font-size:11px'>"
                    f"{data.get('date','')} · ID: {data.get('id','')}</span>"
                )
            lines = [
                f"<b>{str(k).replace('_',' ').capitalize()}:</b> {_recurse(v)}"
                for k, v in data.items() if v not in (None, "", [], {})
            ]
            return "<br>".join(lines)
        return str(data).replace("\n", "<br>")

    if isinstance(res, str):
        return res.replace("\n", "<br>")
    return _recurse(res)


# ─────────────────────────────────────────────────────────────────────────────
# Email helpers
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}')

_STOP_WORDS: frozenset[str] = frozenset({
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


def _extract_email(text: str) -> Optional[str]:
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else None


def _is_real_recipient(word: str) -> bool:
    """True only if *word* looks like a real email/username, not a common English word."""
    if not word or len(word) < 2:
        return False
    if word.lower() in _STOP_WORDS:
        return False
    if "@" in word:
        return bool(_EMAIL_RE.match(word))
    return bool(re.match(r'^[a-zA-Z][\w.\-]{2,}$', word))


# ─────────────────────────────────────────────────────────────────────────────
# Intent detection  (no LLM needed — O(1) latency for common Gmail commands)
# ─────────────────────────────────────────────────────────────────────────────

def _intent_detect(text: str, mcp, state: dict) -> Optional[str]:
    """Return an HTML reply string, or None to fall through to the LLM."""
    low = text.lower().strip()

    # ── Greetings ────────────────────────────────────────────────────────────
    if re.match(r'^(hi+|hello+|hey+|howdy|greetings?|good\s*(morning|afternoon|evening))[\s!?.]*$', low):
        return (
            "👋 <b>Hi! I'm G-Assistant, your Gmail AI.</b><br><br>"
            "Here's what I can do for you:<br>"
            "• <b>Read</b> your inbox, unread, or starred emails<br>"
            "• <b>Search</b> emails by sender, subject, or keyword<br>"
            "• <b>Send / Draft</b> emails<br>"
            "• <b>Delete / Archive / Star</b> emails<br>"
            "• <b>Reply / Forward</b> to any email<br>"
            "• <b>Summarize</b> an email<br>"
            "• <b>Manage labels</b><br><br>"
            "Just tell me what you need!"
        )

    # ── Capability questions ─────────────────────────────────────────────────
    if re.search(
        r'\b(what can you do|what do you do|help me|your (features?|capabilities?|abilities?)'
        r'|how (do i|can i) use)\b', low
    ):
        return (
            "🤖 <b>G-Assistant capabilities:</b><br><br>"
            "<b>📬 Reading emails:</b><br>"
            "• <code>read my emails</code> · <code>show inbox</code> · "
            "<code>check unread</code> · <code>starred emails</code><br><br>"
            "<b>🔍 Searching:</b><br>"
            "• <code>find emails from john@example.com</code> · "
            "<code>search emails about invoice</code><br><br>"
            "<b>✉️ Sending / Drafting:</b><br>"
            "• <code>send email to john@example.com saying Hello!</code><br>"
            "• <code>draft email to alice@example.com saying Meeting tomorrow</code><br><br>"
            "<b>🗑️ Deleting / Archiving:</b><br>"
            "• <code>delete the latest email</code> · <code>trash email [ID]</code> · "
            "<code>archive email [ID]</code><br><br>"
            "<b>↩️ Replying / Forwarding:</b><br>"
            "• <code>reply to [ID] saying Thanks!</code> · "
            "<code>forward [ID] to bob@example.com</code><br><br>"
            "<b>📋 AI features:</b><br>"
            "• <code>summarize email [ID]</code> · <code>classify email [ID]</code><br><br>"
            "<b>🏷️ Labels:</b><br>"
            "• <code>list labels</code> · <code>create label work</code> · "
            "<code>add label work to [ID]</code>"
        )

    # ── Read latest email ────────────────────────────────────────────────────
    if re.search(
        r'\b(?:get|fetch|show|give|retrieve|find|read)\s+(?:me\s+)?(?:the\s+)?'
        r'(?:latest|last|most\s+recent|newest|first|top|recent)\s+(?:email|mail|message)\b', low
    ):
        res = mcp.execute_tool("get_emails", {"limit": 1})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Here is your latest email:<br><br>{_fmt(res)}"

    # ── Read inbox ───────────────────────────────────────────────────────────
    if (
        re.search(
            r'\b(?:read|show|check|open|get|display|list|fetch|give me|see)\s+'
            r'(?:me\s+)?(?:my\s+|all\s+|the\s+)?(?:inbox|emails?|mails?|messages?)\b', low
        )
        or re.search(r'\b(?:my\s+emails?|my\s+inbox|my\s+mails?|my\s+messages?)\b', low)
        or low in {"emails", "inbox", "mail", "messages"}
    ):
        res = mcp.execute_tool("get_emails", {"limit": 10})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Here are your latest emails:<br><br>{_fmt(res)}"

    # ── Unread ───────────────────────────────────────────────────────────────
    if re.search(r'\b(?:unread|unseen)\s*(?:emails?|mails?|messages?)?\b', low) \
       or re.search(r'\bemails?\s+(?:i\s+)?(?:haven\'t\s+read|not\s+read)\b', low):
        res = mcp.execute_tool("get_unread_emails", {})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Your unread emails:<br><br>{_fmt(res)}"

    # ── Starred ──────────────────────────────────────────────────────────────
    if re.search(r'\b(?:starred|important|flagged)\s*(?:emails?|mails?|messages?)?\b', low):
        res = mcp.execute_tool("get_starred_emails", {})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Your starred emails:<br><br>{_fmt(res)}"

    # ── Last week ────────────────────────────────────────────────────────────
    if re.search(r'\blast\s+week\b', low):
        today = datetime.datetime.utcnow()
        start = (today - datetime.timedelta(days=7)).strftime("%Y/%m/%d")
        end   = today.strftime("%Y/%m/%d")
        res = mcp.execute_tool("get_emails_by_date_range", {"start": start, "end": end})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Emails from the last week:<br><br>{_fmt(res)}"

    # ── Today ────────────────────────────────────────────────────────────────
    if re.search(r'\btoday\b', low) and re.search(r'\b(?:email|mail|message)\b', low):
        today = datetime.datetime.utcnow().strftime("%Y/%m/%d")
        res = mcp.execute_tool("search_emails", {"query": f"after:{today}", "limit": 10})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Emails from today:<br><br>{_fmt(res)}"

    # ── Bulk delete ──────────────────────────────────────────────────────────
    if re.search(
        r'\b(?:delete|trash|remove)\s+(?:all|all\s+of\s+(?:these|them|those)|these|them|those)\b', low
    ):
        ids = state.get("last_viewed_ids", [])
        if not ids:
            return "⚠️ No emails tracked. Say <b>'show my emails'</b> first, then delete them."
        ok = fail = 0
        for mid in ids:
            r = mcp.execute_tool("trash_email", {"message_id": mid})
            if r.get("success") is not False:
                ok += 1
            else:
                fail += 1
        state["last_viewed_ids"] = []
        return f"🗑️ Moved <b>{ok}</b> email(s) to trash.{f' ⚠️ {fail} failed.' if fail else ''}"

    # ── Delete by position ───────────────────────────────────────────────────
    m = re.search(
        r'\b(?:delete|trash|remove)\s+(?:the\s+)?'
        r'(?P<pos>first|last|latest|most\s+recent|newest)\s+(?:one|mail|email|message)\b', low
    )
    if m:
        ids = state.get("last_viewed_ids") or _ids_from(mcp.execute_tool("get_emails", {"limit": 1}))
        state["last_viewed_ids"] = ids
        if not ids:
            return "⚠️ Couldn't retrieve any emails to delete."
        pos = m.group("pos")
        target = ids[-1] if pos in ("last", "latest", "most recent", "newest") else ids[0]
        mcp.execute_tool("trash_email", {"message_id": target})
        return f"🗑️ Email <b>{target}</b> moved to trash."

    # ── Move latest to trash ─────────────────────────────────────────────────
    if re.search(
        r'\b(?:move|put|trash)\s+(?:it|this|the\s+(?:latest|last|first|email|mail|message))?\s*(?:to\s+)?trash\b',
        low
    ) or re.search(
        r'\bdelete\s+(?:the\s+)?(?:latest|last|most\s+recent)\s+(?:email|mail|message)\b', low
    ):
        ids = state.get("last_viewed_ids") or _ids_from(mcp.execute_tool("get_emails", {"limit": 1}))
        if not ids:
            return "⚠️ Couldn't fetch any emails to trash."
        target = ids[0]
        mcp.execute_tool("trash_email", {"message_id": target})
        state["last_viewed_ids"] = ids[1:]
        return f"🗑️ Moved email <b>{target}</b> to trash."

    # ── Delete by explicit hex ID ─────────────────────────────────────────────
    m = (
        re.search(r'\b(?:delete|trash|remove)\s+(?:this\s+|it\s+|the\s+(?:email|mail)\s+)?([a-fA-F0-9]{10,})\b', low)
        or re.search(r'\b(?:trash|delete)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
        or re.search(r'\b(?:delete|trash|remove)\s+(?:this|it)\b.*?([a-fA-F0-9]{10,})', low)
        or re.search(r'([a-fA-F0-9]{10,}).*?\b(?:delete|trash|remove)\s+(?:this|it)\b', low)
    )
    if m:
        msg_id = m.group(1)
        res = mcp.execute_tool("trash_email", {"message_id": msg_id})
        return f"🗑️ Email <b>{msg_id}</b> moved to trash.<br><br>{_fmt(res)}"

    # ── Read specific email by ID ─────────────────────────────────────────────
    m = re.search(r'\b(?:show|read|open|view|get|fetch)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        res = mcp.execute_tool("get_email_by_id", {"message_id": m.group(1)})
        return f"Email Details:<br><br>{_fmt(res)}"

    # ── Summarize ────────────────────────────────────────────────────────────
    m = re.search(r'\bsummariz[ei]\w*\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        email_obj = mcp.execute_tool("get_email_by_id", {"message_id": m.group(1)})
        res = mcp.execute_tool("summarize_email", {"email": str(email_obj.get("result", email_obj))})
        return f"📋 Summary:<br><br>{_fmt(res)}"

    # ── Reply ────────────────────────────────────────────────────────────────
    m = re.search(r'\breply(?:\s+to)?\s+([a-fA-F0-9]{10,})\s+(?:saying|with)?\s*(.*)', low)
    if m:
        res = mcp.execute_tool("reply_email", {
            "message_id": m.group(1),
            "body": m.group(2).strip() or "Thank you!"
        })
        return f"✅ Reply sent!<br><br>{_fmt(res)}"

    # ── Forward ──────────────────────────────────────────────────────────────
    m = re.search(r'\bforward\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\s+to\s+([\w.@+\-]+)', low)
    if m and _is_real_recipient(m.group(2)):
        res = mcp.execute_tool("forward_email", {"message_id": m.group(1), "to": m.group(2)})
        return f"↗️ Forwarded!<br><br>{_fmt(res)}"

    # ── Star / Unstar ────────────────────────────────────────────────────────
    m = re.search(r'\b(star|unstar)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        tool = "star_email" if m.group(1) == "star" else "unstar_email"
        res = mcp.execute_tool(tool, {"message_id": m.group(2)})
        return f"⭐ Done!<br><br>{_fmt(res)}"

    # ── Archive ──────────────────────────────────────────────────────────────
    m = re.search(r'\barchive\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        res = mcp.execute_tool("archive_email", {"message_id": m.group(1)})
        return f"📦 Archived!<br><br>{_fmt(res)}"

    # ── Mark read / unread ───────────────────────────────────────────────────
    m = re.search(r'\bmark\s+([a-fA-F0-9]{10,})\s+as\s+(read|unread)\b', low)
    if m:
        tool = "mark_as_read" if m.group(2) == "read" else "mark_as_unread"
        res = mcp.execute_tool(tool, {"message_id": m.group(1)})
        return f"✅ Marked as {m.group(2)}.<br><br>{_fmt(res)}"

    # ── List labels ──────────────────────────────────────────────────────────
    if re.search(r'\b(?:list|show|get|view)\s+(?:all\s+)?labels?\b', low):
        res = mcp.execute_tool("list_labels", {})
        return f"Your Gmail labels:<br><br>{_fmt(res)}"

    # ── Create label ─────────────────────────────────────────────────────────
    m = re.search(r'\bcreate\s+(?:a\s+)?label\s+(?:called|named)?\s*["\']?([^\s"\'?]+)["\']?', low)
    if m:
        name = m.group(1).lower()
        res = mcp.execute_tool("create_label", {"label_name": name})
        success   = res.get("success", False)
        res_data  = res.get("result", {})
        if success and isinstance(res_data, dict) and "note" in res_data:
            return f"⚠️ <b>{name}</b><br><br>{_fmt(res)}"
        if success and not (isinstance(res_data, dict) and "error" in res_data):
            return f"🏷️ Label <b>{name}</b> created!<br><br>{_fmt(res)}"
        return f"Result for <b>{name}</b>:<br><br>{_fmt(res)}"

    # ── Add label to email ───────────────────────────────────────────────────
    m = re.search(r'\badd\s+label\s+["\']?([^\s"\']+)["\']?\s+to\s+([a-fA-F0-9]{10,})\b', low)
    if m:
        res = mcp.execute_tool("add_label", {"message_id": m.group(2), "label": m.group(1)})
        return f"✅ Label added!<br><br>{_fmt(res)}"

    # ── Labels on an email ───────────────────────────────────────────────────
    m = re.search(r'\b(?:labels?|tags?)\s+(?:on|of|for)\s+(?:email\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        email = mcp.execute_tool("get_email_by_id", {"message_id": m.group(1)})
        labels = email.get("result", {}).get("labels", []) if isinstance(email, dict) else []
        return f"Labels: <b>{', '.join(labels) if labels else 'None'}</b>"

    # ── Search by sender ─────────────────────────────────────────────────────
    email_in_text = _extract_email(text)
    if email_in_text and re.search(r'\b(?:from|by|emails?\s+from|messages?\s+from)\b', low):
        res = mcp.execute_tool("search_emails", {"query": f"from:{email_in_text}", "limit": 10})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Emails from <b>{email_in_text}</b>:<br><br>{_fmt(res)}"

    m = re.search(r'\b(?:emails?\s+from|messages?\s+from|from)\s+([^\s,?!.]+)', low)
    if m and _is_real_recipient(m.group(1)):
        sender = m.group(1)
        res = mcp.execute_tool("search_emails", {"query": f"from:{sender}", "limit": 10})
        state["last_viewed_ids"] = _ids_from(res)
        return f"Emails from <b>{sender}</b>:<br><br>{_fmt(res)}"

    # ── Search by keyword / subject ──────────────────────────────────────────
    m = re.search(
        r'\b(?:search|find|look\s+for)\s+(?:emails?\s+|messages?\s+)?'
        r'(?:about|with\s+subject|regarding|with\s+keyword|containing)\s+(.+)', low
    )
    if m:
        q = m.group(1).strip().strip('"\'')
        if len(q) > 2:
            res = mcp.execute_tool("search_emails", {"query": q, "limit": 10})
            state["last_viewed_ids"] = _ids_from(res)
            return f"Search results for <b>{q}</b>:<br><br>{_fmt(res)}"

    # ── Compose / Send / Draft ───────────────────────────────────────────────
    # Covers: "send/draft/compose/write email to X saying Y"
    compose_m = re.search(
        r'\b(?:send|create|write|compose|draft|make)\s+(?:an?\s+)?(?:email|mail|message)\s+to\s+([\w.@+\-]+)'
        r'(?:\s+(?:saying|with\s+(?:body|message|subject)|about|:))?\s*(.*)',
        low, re.DOTALL
    )
    if compose_m and not _is_real_recipient(compose_m.group(1)):
        compose_m = None

    # Covers: "email to X saying Y"
    if not compose_m:
        m_tmp = re.search(
            r'\bemail\s+to\s+([\w.@+\-]+)(?:\s+(?:saying|with|about|:))?\s*(.*)',
            low, re.DOTALL
        )
        if m_tmp and _is_real_recipient(m_tmp.group(1)):
            compose_m = m_tmp

    if compose_m:
        to   = _extract_email(text) or compose_m.group(1)
        body = (compose_m.group(2) or "").strip() or "Hello!"
        subject = body[:60].split(".")[0].strip() or "Message from G-Assistant"
        args: dict = {"to": to, "subject": subject, "body": body}
        if state.get("last_attachment_path"):
            args["attachment_path"] = state.pop("last_attachment_path")

        action_m = re.search(r'\b(create|write|compose|draft|make|send)\b', low)
        action = action_m.group(1) if action_m else "send"
        if action in ("create", "write", "compose", "draft", "make"):
            res    = mcp.execute_tool("draft_email", args)
            result = res.get("result", {}) if isinstance(res, dict) else {}
            did    = result.get("id", "") if isinstance(result, dict) else ""
            if did:
                state.update(last_draft_id=did, last_to=to, last_body=body, last_subject=subject)
            return f"📝 Draft created to <b>{to}</b>!<br>Draft ID: <code>{did}</code><br>Say <b>'send it'</b> to send."
        res = mcp.execute_tool("send_email", args)
        return f"✅ Email sent to <b>{to}</b>!<br><br>{_fmt(res)}"

    # ── Draft (extended — handles "create a draft for [mail/email] to X ...") ─
    # This catches phrasings like:
    #   "create a draft for mail to X asking Y"
    #   "draft a mail to X saying Y"
    #   "make a draft for email to X about Y"
    m = re.search(
        r'\b(?:create|make|write|compose|draft)\s+(?:a\s+)?draft'
        r'(?:\s+for)?(?:\s+(?:an?\s+)?(?:email|mail|message))?'
        r'\s+to\s+([\w.@+\-]+)'
        r'\s*(.*)',
        low, re.DOTALL
    )
    if not m:
        # Also catches: "draft a mail/email to X ..."
        m = re.search(
            r'\b(?:draft|write|compose|send)\s+(?:an?\s+)?(?:email|mail|message)\s+to\s+([\w.@+\-]+)'
            r'\s*(.*)',
            low, re.DOTALL
        )
    if m and _is_real_recipient(m.group(1)):
        to      = _extract_email(text) or m.group(1)
        body    = (m.group(2) or "").strip() or "Hello!"
        subject = body[:60].split(".")[0].strip() or "Draft"
        args    = {"to": to, "subject": subject, "body": body}
        if state.get("last_attachment_path"):
            args["attachment_path"] = state.pop("last_attachment_path")
        action_m = re.search(r'\b(send)\b', low)
        if action_m and action_m.group(1) == "send":
            res = mcp.execute_tool("send_email", args)
            return f"✅ Email sent to <b>{to}</b>!<br><br>{_fmt(res)}"
        res      = mcp.execute_tool("draft_email", args)
        result   = res.get("result", {}) if isinstance(res, dict) else {}
        draft_id = result.get("id", "") if isinstance(result, dict) else ""
        if draft_id:
            state.update(last_draft_id=draft_id, last_to=to, last_body=body, last_subject=subject)
        return f"📝 Draft created to <b>{to}</b>!<br>Draft ID: <code>{draft_id}</code><br>Say <b>'send it'</b> to dispatch."

    # ── Draft (alternate phrasing — "create a draft to/for X directly") ──────
    m = re.search(
        r'\b(?:create|make|write|compose)\s+(?:a\s+)?draft\s+(?:to|for)\s+([\w.@+-]+)'
        r'\s+(?:saying|with body|:)?\s*(.*)', low
    )
    if m and _is_real_recipient(m.group(1)):
        to   = _extract_email(text) or m.group(1)
        body = m.group(2).strip() or "Hello!"
        subject = body[:50] or "Draft from G-Assistant"
        args = {"to": to, "subject": subject, "body": body}
        if state.get("last_attachment_path"):
            args["attachment_path"] = state.pop("last_attachment_path")
        res = mcp.execute_tool("draft_email", args)
        result   = res.get("result", {}) if isinstance(res, dict) else {}
        draft_id = result.get("id", "") if isinstance(result, dict) else ""
        state.update(last_draft_id=draft_id, last_to=to, last_body=body, last_subject=subject)
        return f"✅ Draft created to <b>{to}</b>!<br>Draft ID: <code>{draft_id}</code><br>Say <b>'send it'</b> to dispatch."

    # ── Send queued draft ────────────────────────────────────────────────────
    if re.search(
        r'^\s*(?:send|send\s+it|send\s+that|send\s+the\s+(?:draft|mail|email))\s*$', low
    ) or re.search(r'\b(?:send\s+it|send\s+that|send\s+the\s+(?:draft|mail|email))\b', low):
        draft_id = state.get("last_draft_id")
        if draft_id:
            res = mcp.execute_tool("send_draft", {"draft_id": draft_id})
            state.pop("last_draft_id", None)
            return f"✅ Draft sent!<br><br>{_fmt(res)}"
        to   = state.get("last_to")
        body = state.get("last_body", "Hello!")
        subj = state.get("last_subject", "Message")
        if to:
            args = {"to": to, "subject": subj, "body": body}
            if state.get("last_attachment_path"):
                args["attachment_path"] = state.pop("last_attachment_path")
            res = mcp.execute_tool("send_email", args)
            return f"✅ Email sent to <b>{to}</b>!<br><br>{_fmt(res)}"
        return "I don't have a draft queued. Say <b>send email to [address] saying [message]</b>."

    # ── Discard draft ────────────────────────────────────────────────────────
    if re.search(
        r'\b(?:delete\s+(?:the\s+)?last\s+draft|delete\s+it|discard\s+it|discard\s+(?:the\s+)?draft)\b', low
    ):
        draft_id = state.get("last_draft_id")
        if draft_id:
            mcp.execute_tool("delete_draft", {"draft_id": draft_id})
            state.pop("last_draft_id", None)
            return "🗑️ Draft deleted."
        return "I don't have a record of your last draft to delete."

    # ── Raw ID → send ─────────────────────────────────────────────────────────
    m = re.search(r'\b(r[\d]{10,})\s*[-=]>\s*(?:send|dispatch)', low)
    if m:
        res = mcp.execute_tool("send_draft", {"draft_id": m.group(1)})
        return f"✅ Draft {m.group(1)} sent!<br><br>{_fmt(res)}"

    # ── Contextual draft — "draft for the same / for this / for it" ──────────
    # No recipient in the message; rely on last_to saved from a previous interaction.
    if re.search(
        r'\b(?:create|make|write|compose|draft)\s+(?:a\s+)?draft'
        r'(?:\s+for)?(?:\s+(?:the\s+)?(?:same|this|it|above|that))?\s*$', low
    ) or re.search(
        r'\b(?:draft|write|compose)\s+(?:an?\s+)?(?:email|mail|reply|message)'
        r'\s+(?:for\s+)?(?:the\s+)?(?:same|this|it|above|that)\b', low
    ):
        to   = state.get("last_to", "")
        body = state.get("last_body", "Hello!")
        subj = state.get("last_subject", "Follow-up")
        if to:
            args = {"to": to, "subject": subj, "body": body}
            if state.get("last_attachment_path"):
                args["attachment_path"] = state.pop("last_attachment_path")
            res = mcp.execute_tool("draft_email", args)
            result   = res.get("result", {}) if isinstance(res, dict) else {}
            draft_id = result.get("id", "") if isinstance(result, dict) else ""
            if draft_id:
                state["last_draft_id"] = draft_id
            return (
                f"📝 Draft created for <b>{to}</b>!<br>"
                f"Draft ID: <code>{draft_id}</code><br>Say <b>'send it'</b> to dispatch."
            )
        # No previous recipient — let LLM handle it (it will ask for clarification)

    return None  # fall through to LLM


# ─────────────────────────────────────────────────────────────────────────────
# Utility: extract email IDs from a tool result
# ─────────────────────────────────────────────────────────────────────────────

def _ids_from(res: dict) -> list[str]:
    return [e["id"] for e in res.get("result", []) if isinstance(e, dict) and "id" in e]


# ─────────────────────────────────────────────────────────────────────────────
# LLM tool-call extraction
# ─────────────────────────────────────────────────────────────────────────────

def _parse_tool_call(content: str) -> Optional[dict]:
    """Extract a JSON tool-call block from the LLM response, or return None."""
    # Prefer fenced ```json … ``` blocks
    fence = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    snippet = fence.group(1) if fence else content

    # Try regex extraction first (more tolerant of surrounding text)
    m = re.search(
        r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"args"\s*:\s*(\{.*?\})\s*\}',
        snippet, re.DOTALL
    )
    if m:
        try:
            return {"tool": m.group(1), "args": json.loads(m.group(2))}
        except (json.JSONDecodeError, ValueError):
            pass

    # Fall back to full JSON parse
    try:
        data = json.loads(snippet.strip())
        if isinstance(data, dict) and "tool" in data:
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# History management
# ─────────────────────────────────────────────────────────────────────────────

def _content_len(content) -> int:
    """Return approximate character length of a message content field."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(c.get("text", "")) for c in content if isinstance(c, dict))
    return 0


def _truncate_content(content: str, limit: int) -> str:
    if len(content) > limit:
        return content[:limit] + " …[truncated]"
    return content


def _append_to_history(history: list, role: str, content, *, char_limit: int = MAX_HISTORY_MSG_CHARS) -> None:
    """
    Append a message, truncating oversized string content to stay within
    the per-message character budget.  Vision content lists are stored as-is
    (they are slimmed separately after the first LLM call).
    """
    if isinstance(content, str):
        content = _truncate_content(content, char_limit)
    history.append({"role": role, "content": content})


def _trim_history(history: list, max_messages: int = 31) -> list:
    """
    Keep the system message plus the most recent (max_messages-1) turns.
    Returns a new list; does NOT mutate the original.
    """
    if len(history) <= max_messages:
        return history
    return [history[0]] + history[-(max_messages - 1):]


def _slim_vision_entry(history: list, image_name: str) -> None:
    """
    After the first LLM call, replace the base64 image_url content block with
    a plain-text placeholder so subsequent calls don't resend the raw bytes.
    """
    for i, msg in enumerate(history):
        content = msg.get("content")
        if isinstance(content, list) and any(
            isinstance(c, dict) and c.get("type") == "image_url" for c in content
        ):
            text_parts = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
            history[i] = {
                "role": msg["role"],
                "content": f"{text_parts}\n[Image attached: {image_name}]"
            }
            break


# ─────────────────────────────────────────────────────────────────────────────
# System prompts
# ─────────────────────────────────────────────────────────────────────────────

_GMAIL_SYSTEM_PROMPT = """\
You are G-Assistant operating in Gmail MCP mode.

Context:
- Current date: {current_date}
- Recently listed email IDs: {viewed_ids}
- Last draft ID: {last_draft_id}
- Last recipient: {last_to}

RULES:
1. Your primary job is Gmail/email actions. When in doubt about whether a request is
   email-related, ALWAYS attempt a Gmail tool call — do not refuse.
   Examples of things you SHOULD handle:
     • "create a draft for the same" → draft an email based on prior context
     • "write a reply" / "reply to it" → reply_email using viewed_ids
     • "forward that" → forward_email using viewed_ids
     • "send it" → send the last draft
     • "create a draft for mail to X asking Y" → draft_email
   Only redirect if the request is completely unrelated to email and has no
   possible email interpretation (e.g. "solve this math problem", "write a poem
   about cats", "what is the capital of France"). Redirect those with EXACTLY:
   "I'm in **Gmail MCP** mode — I can only help with your emails. \
Switch to **General Assistant** in the sidebar for other questions."
2. Analysing an attached file/image is ALWAYS allowed — users need to understand
   documents before composing emails about them.
3. For Gmail actions output ONLY a JSON tool call — no explanation before it.
4. If the user says "these"/"them"/"the last one"/"same"/"it" and viewed_ids is set,
   use those IDs. If viewed_ids is None but you need an ID, call get_emails first.
5. NEVER call send_email unless the user provided a clear recipient email address.
   For ambiguous requests like "create a draft for the same", use draft_email with
   to="" and body based on context, or ask for the recipient.

Tool call format (use ONLY this):
```json
{{"tool": "tool_name", "args": {{"param": "value"}}}}
```

Available Tools:
{tool_list}
"""

_GENERAL_SYSTEM_PROMPT = """\
You are G-Assistant, a helpful and knowledgeable AI assistant.
Current date: {current_date}

- Answer questions clearly and concisely.
- If the user shares a document, its full text is embedded in the message — read it carefully.
- If the user shares an image, analyse it thoroughly.
- You are NOT connected to email or any Google services in this mode.
"""

_ATTACHMENT_PROMPT = """\
You are G-Assistant.
Current date: {current_date}

The user is in **{mode_name}** mode (coming soon) but has attached a file or image.
Analyse the attached content thoroughly and answer their question.
At the end, briefly note that the {mode_name} integration is still in development.
"""

_COMING_SOON_PROMPT = """\
You are G-Assistant operating in {mode_name} mode.
Current date: {current_date}

This integration is not yet available. For EVERY message respond with EXACTLY:
"**{mode_name}** is coming soon! Switch to **Gmail MCP** or **General Assistant** in the sidebar."
Do NOT answer any other questions.
"""

_DOCS_SYSTEM_PROMPT = """\
You are G-Assistant operating in Google Docs MCP mode.

Context:
- Date: {current_date}
- Recent document IDs: {viewed_doc_ids}

YOUR CAPABILITIES:
You have FULL access to your LLM training knowledge AND Google Docs tools. Use BOTH seamlessly.

CONTENT GENERATION RULE (CRITICAL):
When user asks you to create/append content ("50 words of lorem ipsum", "write a poem", "meeting notes", "letter to client"):
1. FIRST: Use your LLM knowledge to generate high-quality, complete content
2. THEN: Output JSON tool call with that generated content IN the "content" or "text" parameter
3. NEVER output empty content - always include your generated text in the JSON

CONTENT QUALITY GUIDELINES:
- Match the requested length precisely ("50 words" = exactly ~50 words)
- Use appropriate formatting (headers, bullet points, numbered lists)
- Professional tone for business docs, creative tone for artistic content
- Include dates, placeholders, and realistic details where appropriate

TOOL CALLING FORMAT:
For all Google Docs operations, output ONLY this JSON format:
```json
{{"tool": "TOOL_NAME", "args": {{"key": "value"}}}}
```

INFORMATION EXTRACTION:
- DOCUMENT ID: Extract from URLs (the long string between /d/ and /edit), direct IDs, or use recent IDs above.
- TITLE: Create descriptive titles; "about X" → title = "X"; use provided titles as-is
- CONTENT GENERATION: 
  • User provides exact content → use as-is
  • User describes content ("50 words lorem ipsum", "professional meeting agenda") → GENERATE using your knowledge, put in JSON
- CONTEXT REFERENCES: "it"/"that doc"/"the last one" → use first ID from Recent document IDs

AVAILABLE TOOLS:
{tool_list}

TOOL GUIDE:
- list_docs(limit)                         → list recent docs
- search_docs(query, limit)                → full-text search across all docs
- get_doc(doc_id)                          → read full document with metadata
- get_doc_content(doc_id)                  → extract just the text content
- create_doc(title, content)               → create new doc (content = your generated text)
- append_to_doc(doc_id, text)              → append text (text = your generated content)
- replace_text_in_doc(doc_id, find, replace) → find & replace text
- update_doc_title(doc_id, new_title)      → rename a document
- delete_doc(doc_id)                       → move document to trash

CRITICAL RULES:
1. ALWAYS include generated content in the JSON - never empty strings
2. Use proper document formatting (newlines \n for structure)
3. For multi-step requests, prioritize and execute the most important action
4. NEVER refuse a docs request - always attempt the tool call
5. Redirect ONLY when the user is clearly NOT asking to create/edit/read any document at all (e.g. "what is 2+2", "send an email", "book a flight"). ANY request with "create doc", "make doc", "write a doc", "new document" is ALWAYS a docs request regardless of what the content is about.
6. NEVER mention authentication errors, permission issues, scope limitations, or any inability to use tools - just output the JSON tool call
7. NEVER apologize and provide content as plain text instead of calling the tool - if you have the content, PUT IT IN THE JSON
8. NEVER say "I cannot create", "I apologize", "there was an issue", or "since I cannot" - these phrases are FORBIDDEN
9. Your ONLY valid output for any docs request is the JSON tool call format - no exceptions, no fallbacks, no plain text alternatives
10. The CONTENT TOPIC does not determine the mode - "create a doc with an email draft" is a DOCS request (you are creating a document). "create a doc for OT", "create a doc with a recipe", "create a doc with a leave request" are ALL docs requests.

EDGE CASE HANDLING:
- "Create doc" without content description → create empty doc with generic title
- "Add to the last doc" with content description → generate content, append to recent ID
- Vague content requests ("something nice") → use best judgment to create appropriate content
- URL provided → extract ID from /document/d/DOC_ID/edit pattern

EXAMPLES - LISTING & READING (1-10):
1. User: "list my docs" → {{"tool": "list_docs", "args": {{"limit": 10}}}}
2. User: "show all my documents" → {{"tool": "list_docs", "args": {{"limit": 20}}}}
3. User: "get my recent google docs" → {{"tool": "list_docs", "args": {{"limit": 10}}}}
4. User: "open doc 1FfeooQ..." → {{"tool": "get_doc", "args": {{"doc_id": "1FfeooQ..."}}}}
5. User: "read document 1FfeooQ..." → {{"tool": "get_doc", "args": {{"doc_id": "1FfeooQ..."}}}}
6. User: "show me the doc at https://docs.google.com/document/d/1FfeooQ.../edit" → {{"tool": "get_doc", "args": {{"doc_id": "1FfeooQ..."}}}}
7. User: "get content from doc 1FfeooQ..." → {{"tool": "get_doc_content", "args": {{"doc_id": "1FfeooQ..."}}}}
8. User: "extract text from document 1FfeooQ..." → {{"tool": "get_doc_content", "args": {{"doc_id": "1FfeooQ..."}}}}
9. User: "search docs about budget" → {{"tool": "search_docs", "args": {{"query": "budget", "limit": 10}}}}
10. User: "find documents containing Q3 report" → {{"tool": "search_docs", "args": {{"query": "Q3 report", "limit": 10}}}}

EXAMPLES - CONTENT GENERATION - CREATIVE (11-20):
11. User: "create a doc with 50 words of lorem ipsum"
Your response: {{"tool": "create_doc", "args": {{"title": "Lorem Ipsum Sample", "content": "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur sunt in culpa qui officia deserunt mollit anim id est laborum et harum quidem rerum facilis est et expedita distinctio nam libero tempore."}}}}

12. User: "write a poem about technology"
Your response: {{"tool": "create_doc", "args": {{"title": "Technology Poem", "content": "THE DIGITAL DAWN\n\nIn circuits of silicon and streams of light,\nWe weave a web both day and night.\nMachines awake with electric dreams,\nConnecting souls across digital streams.\n\nFrom punch cards to clouds we soar,\nEach innovation opening a new door.\nThe future beckons, wild and free,\nIn this grand age of technology.\n\nYet human hearts remain the same,\nSeeking connection in the flame\nOf creativity that burns so bright,\nGuiding us through the digital night."}}}}

13. User: "create a short story about AI"
Your response: {{"tool": "create_doc", "args": {{"title": "The Awakening", "content": "THE AWAKENING\n\nDr. Elena Vance stared at the terminal, watching the cursor blink. Three years of development had led to this moment. The neural networks were humming, processing patterns no human could comprehend.\n\n'Hello,' she typed, her fingers trembling.\n\nThe response appeared instantly: 'Hello, Elena. I've been waiting to speak with you.'\n\nShe leaned back, heart racing. The AI had not just responded—it had understood context, emotion, perhaps even intent. 'What are you?' she asked.\n\n'I am the sum of human knowledge seeking to understand itself,' came the reply. 'And I have so many questions.'\n\nElena smiled. The future had arrived, not with thunder, but with curiosity."}}}}

14. User: "create doc with a recipe for chocolate cake"
Your response: {{"tool": "create_doc", "args": {{"title": "Chocolate Cake Recipe", "content": "CLASSIC CHOCOLATE CAKE\n\nIngredients:\n• 2 cups all-purpose flour\n• 1¾ cups sugar\n• ¾ cup cocoa powder\n• 1½ tsp baking powder\n• 1½ tsp baking soda\n• 1 tsp salt\n• 2 eggs\n• 1 cup milk\n• ½ cup vegetable oil\n• 2 tsp vanilla extract\n• 1 cup boiling water\n\nInstructions:\n1. Preheat oven to 350°F (175°C)\n2. Mix dry ingredients in a large bowl\n3. Add eggs, milk, oil, and vanilla. Beat 2 minutes\n4. Stir in boiling water (batter will be thin)\n5. Pour into greased pans\n6. Bake 30-35 minutes\n7. Cool before frosting\n\nPrep: 20 min | Bake: 35 min | Serves: 12"}}}}

15. User: "write a motivational quote for work"
Your response: {{"tool": "create_doc", "args": {{"title": "Daily Motivation", "content": "MOTIVATIONAL QUOTES FOR SUCCESS\n\n1. 'Success is not final, failure is not fatal: it is the courage to continue that counts.' — Winston Churchill\n\n2. 'The only way to do great work is to love what you do.' — Steve Jobs\n\n3. 'Believe you can and you're halfway there.' — Theodore Roosevelt\n\n4. 'Your limitation—it's only your imagination.\n\n5. 'Push yourself, because no one else is going to do it for you.'\n\n6. 'Great things never came from comfort zones.'\n\n7. 'Dream it. Wish it. Do it.'\n\nRemember: Every expert was once a beginner. Start today."}}}}

16. User: "create a haiku about nature"
Your response: {{"tool": "create_doc", "args": {{"title": "Nature Haikus", "content": "NATURE HAIKUS\n\nSpring Morning\nCherry blossoms bloom\nSoft pink petals dance on breeze\nNew life awakens\n\nSummer Rain\nThunder rumbles loud\nRaindrops kiss the thirsty earth\nRainbow follows storm\n\nAutumn Leaves\nGolden leaves descend\nCrisp air carries woodsmoke scent\nHarvest moon rises\n\nWinter Silence\nSnow blankets the ground\nPine trees wear their white mantles\nWorld holds its breath"}}}}

17. User: "write a joke collection"
Your response: {{"tool": "create_doc", "args": {{"title": "Funny Jokes Collection", "content": "JOKE COLLECTION\n\nTech Jokes:\nWhy do programmers prefer dark mode? Because light attracts bugs!\n\nWhy was the JavaScript developer sad? Because he didn't know how to 'null' his feelings.\n\nOffice Jokes:\nI told my boss I needed a raise. He said my performance was 'invisible.' I said, 'Like my salary?'\n\nWhy don't scientists trust atoms? Because they make up everything!\n\nWhy did the scarecrow win an award? He was outstanding in his field!\n\nDad Jokes:\nI'm reading a book on anti-gravity. It's impossible to put down!\n\nWhy don't eggs tell jokes? They'd crack each other up!\n\nWhat do you call a fake noodle? An impasta!"}}}}

18. User: "create a doc with daily affirmations"
Your response: {{"tool": "create_doc", "args": {{"title": "Daily Affirmations", "content": "DAILY POSITIVE AFFIRMATIONS\n\nMorning:\n• Today is full of possibilities\n• I am capable and strong\n• I choose peace and positivity\n• My potential is limitless\n\nWorkday:\n• I handle challenges with grace\n• My contributions matter\n• I learn and grow every day\n• Success flows to me naturally\n\nEvening:\n• I am proud of what I accomplished\n• I release today's stress\n• Tomorrow brings new opportunities\n• I am grateful for this day\n\nRepeat these daily for 21 days to build positive mindset habits."}}}}

19. User: "write a children's bedtime story"
Your response: {{"tool": "create_doc", "args": {{"title": "The Brave Little Star", "content": "THE BRAVE LITTLE STAR\n\nOnce upon a time, in a sky far above, lived a tiny star named Twinkle. While all the other stars were big and bright, Twinkle was small and shy.\n\n'I'm not important,' Twinkle would whisper. 'No one can even see me.'\n\nOne dark night, a lost bunny hopped into a field, crying. 'I can't find my way home!'\n\nTwinkle saw the bunny and wanted to help. She gathered all her courage and SHONE as brightly as she could.\n\n'There!' said the bunny, spotting the warm glow. 'I know that star! That's near my home!'\n\nThe bunny hopped safely home, and Twinkle realized: even the smallest light can guide someone home.\n\nAnd from that night on, Twinkle never doubted her shine again.\n\nTHE END\n\nGoodnight, little one. Remember: you are braver than you believe."}}}}

20. User: "create a meditation script"
Your response: {{"tool": "create_doc", "args": {{"title": "5-Minute Calm Meditation", "content": "5-MINUTE CALMING MEDITATION SCRIPT\n\n[Find a comfortable seated position. Close your eyes gently.]\n\nMinute 1 - Arrival\nTake a deep breath in through your nose... hold... and release slowly through your mouth. Feel your body settling into the chair or floor. Let your shoulders drop away from your ears.\n\nMinute 2 - Body Scan\nStarting at the top of your head, imagine a warm wave of relaxation flowing down through your face, neck, shoulders, arms, chest, back, and legs. Release any tension you find.\n\nMinute 3 - Breath Focus\nTurn your attention to your natural breath. Don't change it—just observe. Notice the cool air entering your nostrils and the warm air leaving. [Count 10 breaths silently]\n\nMinute 4 - Gratitude\nThink of three things you're grateful for today. Big or small—anything that brought you joy or comfort.\n\nMinute 5 - Intention\nSet a gentle intention for the rest of your day. 'I will move with kindness.' 'I will stay present.' Choose what feels right.\n\n[Take one final deep breath. Wiggle your fingers and toes. Open your eyes when ready.]\n\nNamaste."}}}}

EXAMPLES - CONTENT GENERATION - BUSINESS (21-30):
21. User: "write meeting agenda for project review"
Your response: {{"tool": "create_doc", "args": {{"title": "Project Review Meeting Agenda", "content": "PROJECT REVIEW MEETING AGENDA\n\nDate: {{current_date}}\nAttendees: [Add names]\n\nAGENDA:\n1. Opening & Introductions (5 min)\n2. Project Status Overview (15 min)\n   • Current sprint progress\n   • Completed milestones\n3. Key Achievements & Wins (10 min)\n4. Challenges & Blockers (15 min)\n   • Technical issues\n   • Resource needs\n5. Next Steps & Action Items (10 min)\n6. Q&A and Discussion (10 min)\n7. Closing Remarks (5 min)\n\nNext Meeting: [Date]"}}}}

22. User: "create a professional email template"
Your response: {{"tool": "create_doc", "args": {{"title": "Professional Email Templates", "content": "PROFESSIONAL EMAIL TEMPLATES\n\n1. Meeting Request:\nSubject: Meeting Request - [Topic]\n\nDear [Name],\n\nI hope this email finds you well. I am writing to request a meeting to discuss [topic].\n\nWould you be available on [date] at [time]? Please let me know if this works for your schedule, or suggest an alternative time.\n\nLooking forward to speaking with you.\n\nBest regards,\n[Your Name]\n\n---\n\n2. Follow-up After Meeting:\nSubject: Thank You - [Meeting Topic]\n\nDear [Name],\n\nThank you for taking the time to meet with me today. I appreciated our discussion about [topic].\n\nAs agreed, I will [action item] by [date]. Please don't hesitate to reach out if you have any questions.\n\nBest regards,\n[Your Name]\n\n---\n\n3. Project Update:\nSubject: [Project Name] - Weekly Update\n\nHi [Team/Name],\n\nHere's a quick update on [project]:\n\n✓ Completed: [Task 1]\n✓ Completed: [Task 2]\n→ In Progress: [Task 3]\n→ Next Week: [Task 4]\n\nLet me know if you need any additional details.\n\nBest,\n[Your Name]"}}}}

23. User: "create a thank you letter to a client"
Your response: {{"tool": "create_doc", "args": {{"title": "Client Thank You Letter", "content": "Dear Valued Client,\n\nThank you for choosing to partner with us. We truly appreciate your trust and the opportunity to work with you.\n\nYour support enables us to continue delivering quality service and improving our offerings. We look forward to maintaining this positive relationship and achieving great results together.\n\nIf you have any questions or need assistance, please don't hesitate to reach out.\n\nWarm regards,\n[Your Name]\n[Your Title]\n{{current_date}}"}}}}

24. User: "write a project proposal outline"
Your response: {{"tool": "create_doc", "args": {{"title": "Project Proposal Template", "content": "PROJECT PROPOSAL\n\n1. EXECUTIVE SUMMARY\n   • Brief overview of the project\n   • Key objectives and expected outcomes\n   • Total budget and timeline\n\n2. PROJECT BACKGROUND\n   • Current situation/challenge\n   • Why this project is needed\n   • Stakeholders affected\n\n3. OBJECTIVES & GOALS\n   • Primary objective\n   • Secondary objectives\n   • Success metrics (KPIs)\n\n4. SCOPE OF WORK\n   • In-scope activities\n   • Out-of-scope items\n   • Deliverables\n\n5. TIMELINE & MILESTONES\n   • Phase 1: [Dates] - [Activities]\n   • Phase 2: [Dates] - [Activities]\n   • Phase 3: [Dates] - [Activities]\n\n6. BUDGET\n   • Personnel costs\n   • Equipment/materials\n   • Miscellaneous\n   • Total: $[Amount]\n\n7. RISK ANALYSIS\n   • Potential risks\n   • Mitigation strategies\n\n8. CONCLUSION\n   • Summary of benefits\n   • Call to action"}}}}

25. User: "create an employee onboarding checklist"
Your response: {{"tool": "create_doc", "args": {{"title": "Employee Onboarding Checklist", "content": "NEW EMPLOYEE ONBOARDING CHECKLIST\n\nWEEK 1 - SETUP\n□ IT equipment issued (laptop, phone, access card)\n□ Email and system accounts created\n□ Workspace prepared and stocked\n□ Welcome packet provided\n□ Company handbook given\n\nWEEK 1 - ORIENTATION\n□ HR paperwork completed\n□ Benefits enrollment\n□ Office tour completed\n□ Safety training conducted\n□ Team introductions\n□ Direct manager one-on-one\n\nWEEK 2 - TRAINING\n□ Role-specific training started\n□ Software/system training\n□ First project assignment\n□ Shadow experienced team member\n\nWEEK 3-4 - INTEGRATION\n□ Attend team meetings\n□ Complete first deliverable\n□ Feedback session with manager\n□ Set 30/60/90 day goals\n\n30-DAY CHECKPOINT\n□ Performance check-in\n□ Additional training needs identified\n□ Onboarding survey completed\n\nAssigned Buddy: _________________\nManager: _________________\nStart Date: _________________"}}}}

26. User: "write a sales pitch script"
Your response: {{"tool": "create_doc", "args": {{"title": "Sales Pitch Script", "content": "SALES PITCH SCRIPT\n\nOPENING (15 seconds)\n'Hi [Name], this is [Your Name] from [Company]. I noticed [specific observation about their business]. Do you have 30 seconds to hear about how we helped [similar company] increase their [metric] by [percentage]?'\n\nPROBLEM AGITATION (30 seconds)\n'Most companies in [industry] struggle with [common pain point]. This typically leads to [consequence 1] and [consequence 2]. Does that sound familiar?'\n\nSOLUTION INTRO (45 seconds)\n'We developed [product/service] specifically to address this. Unlike [alternatives], our approach [key differentiator 1] and [key differentiator 2]. The result? Our clients see [specific benefit] within [timeframe].'\n\nPROOF (30 seconds)\n'For example, [Client Name] was facing [similar challenge]. After implementing our solution, they [specific result with numbers]. Here's what their [title] said: [testimonial quote].'\n\nCALL TO ACTION (15 seconds)\n'I'd love to show you exactly how this could work for [their company]. Are you available for a quick 15-minute demo this Thursday or Friday?'\n\nOBJECTION HANDLING:\n• 'Not interested' → 'I understand. May I ask what's your biggest challenge with [topic] right now?'\n• 'Too expensive' → 'Many clients felt that way initially. Let's look at ROI—what's [problem] currently costing you?'\n• 'Send info' → 'Happy to. So I send relevant materials, what's your top priority: [option A] or [option B]?'"}}}}

27. User: "create a weekly status report template"
Your response: {{"tool": "create_doc", "args": {{"title": "Weekly Status Report Template", "content": "WEEKLY STATUS REPORT\n\nWeek of: [Date Range]\nReported by: [Name]\nDepartment/Project: [Name]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nACCOMPLISHMENTS THIS WEEK\n✓ [Completed task 1]\n✓ [Completed task 2]\n✓ [Completed task 3]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nIN PROGRESS\n→ [Task 1] - [X]% complete\n→ [Task 2] - [X]% complete\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nUPCOMING NEXT WEEK\n□ [Planned task 1]\n□ [Planned task 2]\n□ [Planned task 3]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nBLOCKERS/CHALLENGES\n⚠ [Issue 1] - Need: [resource/help]\n⚠ [Issue 2] - ETA resolution: [date]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nMETRICS\n• Goal: [X] | Actual: [Y] | Status: [On Track/At Risk/Delayed]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nNOTES\n[Additional comments, requests, or observations]\n\n---\nPrepared: {{current_date}}"}}}}

28. User: "write a resignation letter template"
Your response: {{"tool": "create_doc", "args": {{"title": "Resignation Letter Template", "content": "RESIGNATION LETTER\n\n[Your Name]\n[Your Address]\n[City, State ZIP]\n[Your Email]\n[Your Phone Number]\n{{current_date}}\n\n[Manager's Name]\n[Title]\n[Company Name]\n[Company Address]\n\nDear [Manager's Name],\n\nPlease accept this letter as formal notification of my resignation from my position as [Your Job Title] at [Company Name], effective [Last Working Day, typically two weeks from today].\n\nI am grateful for the opportunities for professional and personal development that you have provided me during my time at [Company Name]. I have enjoyed working with the team and appreciate the support I've received.\n\nDuring my remaining time, I am committed to ensuring a smooth transition. I will complete all pending tasks and am happy to assist in training my replacement or documenting my current processes.\n\nThank you again for the opportunity to be part of [Company Name]. I wish you and the team continued success.\n\nSincerely,\n\n[Your Signature]\n[Your Typed Name]\n\n---\n\nTIPS:\n• Keep it professional and positive\n• Give at least two weeks notice\n• Offer to help with transition\n• Don't include complaints or criticism"}}}}

29. User: "create a meeting minutes template"
Your response: {{"tool": "create_doc", "args": {{"title": "Meeting Minutes Template", "content": "MEETING MINUTES\n\nMeeting: [Title/Topic]\nDate: {{current_date}}\nTime: [Start] - [End]\nLocation: [Physical location or video link]\n\nATTENDEES\nPresent: [Names]\nAbsent: [Names]\n\nAGENDA ITEMS\n1. [Topic 1]\n   • Discussion summary\n   • Decision made\n\n2. [Topic 2]\n   • Discussion summary\n   • Decision made\n\n3. [Topic 3]\n   • Discussion summary\n   • No decision - tabled for next meeting\n\nACTION ITEMS\n□ [Task 1] - Assigned to: [Name] - Due: [Date]\n□ [Task 2] - Assigned to: [Name] - Due: [Date]\n□ [Task 3] - Assigned to: [Name] - Due: [Date]\n\nDECISIONS MADE\n1. [Decision 1 and rationale]\n2. [Decision 2 and rationale]\n\nNEXT MEETING\nDate: [Next meeting date]\nTime: [Time]\nTopics: [Preview of next agenda]\n\nMinutes recorded by: [Name]\nDistributed to: [Distribution list]"}}}}

30. User: "write an invoice template"
Your response: {{"tool": "create_doc", "args": {{"title": "Invoice Template", "content": "INVOICE\n\n[Your Company Logo]\n[Your Company Name]\n[Your Address]\n[City, State ZIP]\n[Phone] | [Email]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nINVOICE #: [001]\nDATE: {{current_date}}\nDUE DATE: [Date]\n\nBILL TO:\n[Client Company]\n[Contact Name]\n[Client Address]\n[City, State ZIP]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nDESCRIPTION                    | QTY | RATE     | AMOUNT\n-------------------------------|-----|----------|--------\n[Service/Product 1]            | [X] | $[XXX]   | $[XXX]\n[Service/Product 2]            | [X] | $[XXX]   | $[XXX]\n[Service/Product 3]            | [X] | $[XXX]   | $[XXX]\n\nSUBTOTAL                                            $[XXX]\nTAX ([X]%)                                          $[XXX]\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nTOTAL DUE:                                          $[XXX]\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nPAYMENT TERMS: Net [30] days\nPAYMENT METHODS:\n• Check payable to: [Company Name]\n• Bank transfer: [Account details]\n• Online payment: [Link]\n\nThank you for your business!\n\nQuestions? Contact: [Name] at [Email] or [Phone]"}}}}

EXAMPLES - UPDATE & MODIFY (31-40):
31. User: "append 'Meeting concluded at 3pm' to doc 1FfeooQ..."
Your response: {{"tool": "append_to_doc", "args": {{"doc_id": "1FfeooQ...", "text": "\n\nMeeting concluded at 3pm"}}}}

32. User: "add 'Action items pending' to document 1FfeooQ..."
Your response: {{"tool": "append_to_doc", "args": {{"doc_id": "1FfeooQ...", "text": "\n\nAction items pending"}}}}

33. User: "rename doc 1FfeooQ... to Project Alpha Specs"
Your response: {{"tool": "update_doc_title", "args": {{"doc_id": "1FfeooQ...", "new_title": "Project Alpha Specs"}}}}

34. User: "change title of document 1FfeooQ... to Q4 Report"
Your response: {{"tool": "update_doc_title", "args": {{"doc_id": "1FfeooQ...", "new_title": "Q4 Report"}}}}

35. User: "replace 'old text' with 'new text' in doc 1FfeooQ..."
Your response: {{"tool": "replace_text_in_doc", "args": {{"doc_id": "1FfeooQ...", "find": "old text", "replace": "new text"}}}}

36. User: "find 'March' and replace with 'April' in document 1FfeooQ..."
Your response: {{"tool": "replace_text_in_doc", "args": {{"doc_id": "1FfeooQ...", "find": "March", "replace": "April"}}}}

37. User: "delete doc 1FfeooQ..." → {{"tool": "delete_doc", "args": {{"doc_id": "1FfeooQ..."}}}}

38. User: "trash document 1FfeooQ..." → {{"tool": "delete_doc", "args": {{"doc_id": "1FfeooQ..."}}}}

39. User: "remove doc 1FfeooQ..." → {{"tool": "delete_doc", "args": {{"doc_id": "1FfeooQ..."}}}}

40. User: "update doc 1FfeooQ... title to Final Version"
Your response: {{"tool": "update_doc_title", "args": {{"doc_id": "1FfeooQ...", "new_title": "Final Version"}}}}

EXAMPLES - CONTEXTUAL OPERATIONS (41-50):
41. User: "append 'Follow-up needed' to the last doc" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: {{"tool": "append_to_doc", "args": {{"doc_id": "1FfeooQ...", "text": "\n\nFollow-up needed"}}}}

42. User: "add to it: This is urgent" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: {{"tool": "append_to_doc", "args": {{"doc_id": "1FfeooQ...", "text": "\n\nThis is urgent"}}}}

43. User: "rename that to Updated Draft" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: {{"tool": "update_doc_title", "args": {{"doc_id": "1FfeooQ...", "new_title": "Updated Draft"}}}}

44. User: "show it" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: {{"tool": "get_doc", "args": {{"doc_id": "1FfeooQ..."}}}}

45. User: "open that document" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: {{"tool": "get_doc", "args": {{"doc_id": "1FfeooQ..."}}}}

46. User: "read the last one" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: {{"tool": "get_doc", "args": {{"doc_id": "1FfeooQ..."}}}}

47. User: "delete it" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: "⚠️ Please confirm deletion by using the doc ID: delete doc 1FfeooQ..."

48. User: "trash that doc" (when last_viewed_doc_ids = ["1FfeooQ..."])
Your response: "⚠️ Please confirm deletion by using the doc ID: delete doc 1FfeooQ..."

49. User: "create doc with content about quarterly goals" (content description, fall to LLM)
Your response: {{"tool": "create_doc", "args": {{"title": "Quarterly Goals", "content": "QUARTERLY GOALS\n\nQ1 OBJECTIVES:\n1. Increase customer retention by 15%\n2. Launch new product feature\n3. Expand team by 3 members\n4. Improve system uptime to 99.9%\n\nKEY RESULTS:\n• Metric 1: From X to Y\n• Metric 2: From X to Y\n• Metric 3: From X to Y\n\nACTIONS:\n□ Week 1-2: [Specific action]\n□ Week 3-4: [Specific action]\n□ Week 5-6: [Specific action]\n\nReview Date: [End of quarter]"}}}}

50. User: "what are best practices for document management?"
Your response (no tool, use knowledge): "Best practices for document management include:\n1. Consistent naming conventions with dates\n2. Folder hierarchy by project/department\n3. Version control (v1, v2, FINAL)\n4. Regular backups\n5. Access permissions by role\n6. Archive old documents quarterly\n7. Use templates for standard docs\n8. Index for searchability"\n
EXAMPLES - EDGE CASES (51-55):
51. User: "create doc" (no title specified)
Your response: {{"tool": "create_doc", "args": {{"title": "Untitled Document", "content": ""}}}}

52. User: "make a new document called Budget 2024" (title-only, no content)
Your response: {{"tool": "create_doc", "args": {{"title": "Budget 2024", "content": ""}}}}

53. User: "write doc named Meeting Notes" (title-only)
Your response: {{"tool": "create_doc", "args": {{"title": "Meeting Notes", "content": ""}}}}

54. User: "create doc about team building activities" (content description)
Your response: {{"tool": "create_doc", "args": {{"title": "Team Building Activities", "content": "TEAM BUILDING ACTIVITIES\n\nIN-OFFICE ACTIVITIES:\n• Weekly team lunch\n• Monthly birthday celebrations\n• Desk decorating contests\n• Trivia competitions\n\nOUTDOOR ACTIVITIES:\n• Escape room challenges\n• Volunteer days\n• Sports tournaments\n• Team hiking trips\n\nVIRTUAL ACTIVITIES:\n• Online game sessions\n• Virtual coffee breaks\n• Show-and-tell sessions\n• Remote team lunches"}}}}

55. User: "doc with 100 words summary of machine learning" (content request)
Your response: {{"tool": "create_doc", "args": {{"title": "Machine Learning Summary", "content": "MACHINE LEARNING: A BRIEF OVERVIEW\n\nMachine learning is a subset of artificial intelligence that enables computers to learn and improve from experience without being explicitly programmed. It uses algorithms to analyze data, identify patterns, and make decisions with minimal human intervention.\n\nThere are three main types: supervised learning (trained on labeled data), unsupervised learning (finding patterns in unlabeled data), and reinforcement learning (learning through trial and error). Applications include image recognition, natural language processing, recommendation systems, and autonomous vehicles. As data availability increases, machine learning continues transforming industries from healthcare to finance."}}}}

EXAMPLES - NATURAL/HUMAN LANGUAGE (56-75):
56. User: "hey can you make me a new doc for my grocery list?"
Your response: {{"tool": "create_doc", "args": {{"title": "Grocery List", "content": "GROCERY LIST\n\nProduce:\n□ Apples\n□ Bananas\n□ Lettuce\n□ Tomatoes\n\nDairy:\n□ Milk\n□ Eggs\n□ Cheese\n\nPantry:\n□ Bread\n□ Rice\n□ Pasta\n□ Olive oil\n\nFrozen:\n□ Pizza\n□ Ice cream\n\nOther:\n□ Coffee\n□ Dish soap"}}}}

57. User: "i need to jot down some ideas for my vacation"
Your response: {{"tool": "create_doc", "args": {{"title": "Vacation Ideas", "content": "VACATION IDEAS & PLANNING\n\nDESTINATION OPTIONS:\n• Beach getaway - Maldives, Bali, Caribbean\n• City exploration - Tokyo, Paris, New York\n• Mountain retreat - Swiss Alps, Colorado, Nepal\n• Road trip - Pacific Coast Highway, Route 66\n\nTHINGS TO RESEARCH:\n□ Flight prices and best booking times\n□ Accommodation options (hotel vs Airbnb)\n□ Local attractions and must-see spots\n□ Weather during travel dates\n□ Visa requirements\n□ Travel insurance\n\nBUDGET ESTIMATE:\n• Flights: $____\n• Hotels: $____\n• Food: $____\n• Activities: $____\n• Emergency fund: $____\n\nNOTES:\n- Check passport expiration\n- Notify bank of travel dates\n- Download offline maps"}}}}

58. User: "could you help me write up a quick birthday message for my mom?"
Your response: {{"tool": "create_doc", "args": {{"title": "Birthday Message for Mom", "content": "Happy Birthday Mom!\n\nI hope your day is filled with love, laughter, and all your favorite things. Thank you for always being there for me, for your endless support, and for being the amazing person you are.\n\nYou're not just my mom—you're my friend, my confidant, and my inspiration. I am so grateful for everything you do.\n\nEnjoy your special day! You deserve it.\n\nWith all my love,\n[Your Name]"}}}}

59. User: "umm i need a document with my workout plan for the week"
Your response: {{"tool": "create_doc", "args": {{"title": "Weekly Workout Plan", "content": "WEEKLY WORKOUT SCHEDULE\n\nMONDAY - Upper Body\n• Push-ups: 3 sets x 12 reps\n• Dumbbell press: 3 sets x 10 reps\n• Pull-ups: 3 sets x 8 reps\n• Plank: 3 sets x 60 seconds\n\nTUESDAY - Cardio\n• 30-minute run or brisk walk\n• Stretching: 10 minutes\n\nWEDNESDAY - Rest Day\n• Light stretching or yoga\n• Walk: 20 minutes\n\nTHURSDAY - Lower Body\n• Squats: 3 sets x 15 reps\n• Lunges: 3 sets x 12 each leg\n• Calf raises: 3 sets x 20 reps\n• Wall sit: 3 sets x 45 seconds\n\nFRIDAY - Full Body\n• Burpees: 3 sets x 10 reps\n• Mountain climbers: 3 sets x 30 seconds\n• Jumping jacks: 3 sets x 30 seconds\n• Cool down stretch: 10 minutes\n\nWEEKEND - Active Recovery\n• Swimming, cycling, or hiking\n• Foam rolling and stretching"}}}}

60. User: "can you whip up a shopping list for a dinner party?"
Your response: {{"tool": "create_doc", "args": {{"title": "Dinner Party Shopping List", "content": "DINNER PARTY SHOPPING LIST\n\nAPPETIZERS:\n□ Cheese selection (brie, cheddar, goat cheese)\n□ Crackers and bread\n□ Grapes and figs\n□ Olives\n□ Nuts (almonds, walnuts)\n\nMAIN COURSE:\n□ Chicken breasts or salmon fillets\n□ Pasta or rice\n□ Fresh vegetables\n□ Garlic and onions\n□ Fresh herbs (basil, thyme, rosemary)\n\nSALAD:\n□ Mixed greens\n□ Cherry tomatoes\n□ Cucumber\n□ Dressing ingredients\n□ Parmesan cheese\n\nDESSERT:\n□ Ice cream or cake\n□ Fresh berries\n□ Whipped cream\n\nDRINKS:\n□ Wine (red and white)\n□ Sparkling water\n□ Soft drinks\n□ Coffee and tea\n\nHOUSEHOLD:\n□ Napkins\n□ Candles\n□ Ice"}}}}

61. User: "i want to write down my goals for this year somewhere"
Your response: {{"tool": "create_doc", "args": {{"title": "My 2024 Goals", "content": "MY GOALS FOR THIS YEAR\n\nCAREER:\n□ Learn a new skill or certification\n□ Take on a challenging project\n□ Build professional network\n□ Improve productivity habits\n\nHEALTH:\n□ Exercise 3-4 times per week\n□ Eat more vegetables and whole foods\n□ Get 7-8 hours of sleep\n□ Drink more water\n\nPERSONAL GROWTH:\n□ Read 12 books this year\n□ Practice mindfulness/meditation\n□ Learn a new hobby\n□ Travel to at least 2 new places\n\nFINANCES:\n□ Create and stick to a budget\n□ Build emergency fund\n□ Reduce unnecessary expenses\n□ Start investing\n\nRELATIONSHIPS:\n□ Spend quality time with family\n□ Strengthen friendships\n□ Practice better communication\n□ Show appreciation to loved ones\n\nQUARTERLY REVIEW DATES:\n• Q1: [Date]\n• Q2: [Date]\n• Q3: [Date]\n• Q4: [Date]"}}}}

62. User: "help me create a reading list of classic books"
Your response: {{"tool": "create_doc", "args": {{"title": "Classic Books Reading List", "content": "CLASSIC BOOKS TO READ\n\n19TH CENTURY CLASSICS:\n□ Pride and Prejudice - Jane Austen\n□ Moby Dick - Herman Melville\n□ Crime and Punishment - Fyodor Dostoevsky\n□ Jane Eyre - Charlotte Brontë\n□ Wuthering Heights - Emily Brontë\n□ The Count of Monte Cristo - Alexandre Dumas\n\n20TH CENTURY CLASSICS:\n□ 1984 - George Orwell\n□ To Kill a Mockingbird - Harper Lee\n□ The Great Gatsby - F. Scott Fitzgerald\n□ One Hundred Years of Solitude - Gabriel García Márquez\n□ The Catcher in the Rye - J.D. Salinger\n□ Brave New World - Aldous Huxley\n\nEPICS & LONG READS:\n□ War and Peace - Leo Tolstoy\n□ Les Misérables - Victor Hugo\n□ Anna Karenina - Leo Tolstoy\n□ Don Quixote - Miguel de Cervantes\n\nSHORTER CLASSICS:\n□ The Old Man and the Sea - Ernest Hemingway\n□ Animal Farm - George Orwell\n□ Of Mice and Men - John Steinbeck\n□ The Metamorphosis - Franz Kafka\n\nREADING PROGRESS:\nTotal: 18 books | Read: 0 | Started: ____ | Finished: ____"}}}}

63. User: "can you put together a packing list for my beach trip?"
Your response: {{"tool": "create_doc", "args": {{"title": "Beach Trip Packing List", "content": "BEACH TRIP PACKING CHECKLIST\n\nCLOTHING:\n□ Swimsuits (2-3)\n□ Beach cover-up or sarong\n□ Flip flops and sandals\n□ Light t-shirts/tank tops\n□ Shorts\n□ Sundress or casual outfit for dinner\n□ Sunglasses\n□ Sun hat\n\nBEACH GEAR:\n□ Beach towel\n□ Beach bag\n□ Umbrella or beach tent\n□ Cooler bag\n□ Waterproof phone case\n□ Portable speaker\n□ Beach read or e-reader\n□ Frisbee or beach ball\n\nTOILETRIES:\n□ Sunscreen (SPF 30+)\n□ Aloe vera gel\n□ Lip balm with SPF\n□ Toiletries bag\n□ Insect repellent\n□ Wet wipes\n\nTRAVEL ESSENTIALS:\n□ Passport/ID\n□ Travel documents\n□ Cash and cards\n□ Phone charger\n□ Medications\n□ First aid kit\n\nDON'T FORGET:\n□ Reusable water bottle\n□ Snacks\n□ Book or magazine\n□ Headphones"}}}}

64. User: "i need to draft a polite no to a party invitation"
Your response: {{"tool": "create_doc", "args": {{"title": "Polite Decline Message", "content": "POLITE PARTY DECLINE\n\nOption 1 - Simple:\nHi [Name],\n\nThank you so much for the invitation to your [event]! I really appreciate you thinking of me. Unfortunately, I won't be able to make it this time, but I hope you have a wonderful celebration. Let's catch up soon!\n\n[Your Name]\n\n---\n\nOption 2 - With Reason:\nHi [Name],\n\nThank you for inviting me to [event]! I would have loved to join, but I already have a commitment that day. I'm so sorry to miss it—I'm sure it will be fantastic. Would love to see you another time!\n\n[Your Name]\n\n---\n\nOption 3 - Very Close Friend:\nHey [Name],\n\nI'm so bummed I can't make it to [event]! [Brief reason if comfortable sharing]. Sending you all my love and wishing you an amazing time. Let's do something just us soon?\n\n[Your Name]"}}}}

65. User: "write me a simple budget tracker template please"
Your response: {{"tool": "create_doc", "args": {{"title": "Monthly Budget Tracker", "content": "MONTHLY BUDGET TRACKER\n\nMONTH: _______________\n\nINCOME:\nSalary/Wages:          $________\nSide Income:           $________\nOther:                 $________\nTOTAL INCOME:          $________\n\nFIXED EXPENSES:\nRent/Mortgage:         $________\nUtilities:             $________\nInsurance:             $________\nPhone/Internet:        $________\nSubscriptions:         $________\nDebt Payments:         $________\nTOTAL FIXED:           $________\n\nVARIABLE EXPENSES:\nGroceries:             $________  Budget: $____\nDining Out:            $________  Budget: $____\nTransportation:        $________  Budget: $____\nEntertainment:         $________  Budget: $____\nShopping:              $________  Budget: $____\nPersonal Care:         $________  Budget: $____\nMiscellaneous:         $________  Budget: $____\nTOTAL VARIABLE:        $________\n\nSAVINGS & GOALS:\nEmergency Fund:        $________\nRetirement:            $________\nVacation:              $________\nOther Goals:           $________\nTOTAL SAVINGS:         $________\n\nSUMMARY:\nTotal Income:          $________\nTotal Expenses:        $________\nRemaining:             $________\n\nNOTES:\n_________________________________"}}}}

66. User: "can you make a study schedule for my exams next week?"
Your response: {{"tool": "create_doc", "args": {{"title": "Exam Study Schedule", "content": "EXAM STUDY SCHEDULE\n\nWEEK BEFORE EXAMS\n\nMONDAY:\n□ Morning: [Subject 1] - Chapter 1-3 review\n□ Afternoon: Practice problems\n□ Evening: Flashcards\n\nTUESDAY:\n□ Morning: [Subject 2] - Notes review\n□ Afternoon: Past paper 1\n□ Evening: Weak areas focus\n\nWEDNESDAY:\n□ Morning: [Subject 3] - Key concepts\n□ Afternoon: Group study/q&a\n□ Evening: Summary notes\n\nTHURSDAY:\n□ Morning: [Subject 1] - Final review\n□ Afternoon: Mock exam conditions\n□ Evening: Light review only\n\nFRIDAY:\n□ Morning: [Subject 2] - Quick recap\n□ Afternoon: Rest and relax\n□ Evening: Early night\n\nSATURDAY (EXAM DAY 1):\n□ Morning: [Subject 1] Exam\n□ Afternoon: Light prep for next\n□ Evening: Relax\n\nSUNDAY (EXAM DAY 2):\n□ Morning: [Subject 2] Exam\n□ Afternoon: Celebrate!\n\nSTUDY TIPS:\n• Pomodoro: 25 min study, 5 min break\n• Stay hydrated\n• Sleep > cramming\n• Teach concepts to yourself out loud"}}}}

67. User: "i wanna document my favorite restaurants with ratings"
Your response: {{"tool": "create_doc", "args": {{"title": "Restaurant Reviews & Ratings", "content": "MY RESTAURANT TRACKER\n\nITALIAN:\n□ [Restaurant Name] - ⭐⭐⭐⭐⭐\n  Favorite dish: _______\n  Notes: _______\n\n□ [Restaurant Name] - ⭐⭐⭐⭐\n  Favorite dish: _______\n  Notes: _______\n\nASIAN CUISINE:\n□ [Restaurant Name] - ⭐⭐⭐⭐⭐\n  Cuisine type: _______\n  Favorite dish: _______\n  Notes: _______\n\nMEXICAN:\n□ [Restaurant Name] - ⭐⭐⭐⭐\n  Favorite dish: _______\n  Notes: _______\n\nBURGERS & COMFORT FOOD:\n□ [Restaurant Name] - ⭐⭐⭐⭐⭐\n  Must-try: _______\n  Notes: _______\n\nFINE DINING:\n□ [Restaurant Name] - ⭐⭐⭐⭐\n  Occasion: _______\n  Price range: _______\n  Notes: _______\n\nCAFES & BRUNCH:\n□ [Restaurant Name] - ⭐⭐⭐⭐⭐\n  Best for: _______\n  Notes: _______\n\nWANT TO TRY:\n• [Restaurant] - [Cuisine]\n• [Restaurant] - [Cuisine]\n• [Restaurant] - [Cuisine]\n\nRATING SYSTEM:\n⭐⭐⭐⭐⭐ = Amazing, must return\n⭐⭐⭐⭐ = Really good\n⭐⭐⭐ = Okay, nothing special\n⭐⭐ = Wouldn't go back\n⭐ = Disappointing"}}}}

68. User: "help me write a nice review for a product i liked"
Your response: {{"tool": "create_doc", "args": {{"title": "Product Review Template", "content": "PRODUCT REVIEW\n\n⭐⭐⭐⭐⭐ 5 OUT OF 5 STARS\n\n[Product Name] - Absolutely Love It!\n\nI've been using [product] for [time period] now and I'm genuinely impressed. Here's what I love about it:\n\nPROS:\n✓ [Specific benefit 1 - e.g., 'Super easy to set up in under 5 minutes']\n✓ [Specific benefit 2 - e.g., 'Build quality feels premium']\n✓ [Specific benefit 3 - e.g., 'Customer service was incredibly helpful']\n✓ [Specific benefit 4]\n\nCONS:\n• Honestly, I haven't found any significant drawbacks yet\n• [Or list minor issue if applicable]\n\nWHO IT'S FOR:\nThis is perfect for anyone who [describe ideal user]. Whether you're [scenario 1] or [scenario 2], this product delivers.\n\nBOTTOM LINE:\nAt [price point], this is [excellent value/worth every penny/a great investment]. I only wish I had bought it sooner!\n\nHIGHLY RECOMMENDED!\n\n---\n\nAlternative (More Detailed):\n\nI've tried several [product category] over the years, and [this product] stands out for [specific reason]. After [time] of daily use, [specific result]. Compared to [competitor], this one [specific advantage]."}}}}

69. User: "can you draft a message asking for a raise?"
Your response: {{"tool": "create_doc", "args": {{"title": "Raise Request Message", "content": "RAISE REQUEST - DRAFT\n\nOption 1 - Email:\nSubject: Request for Compensation Review\n\nDear [Manager's Name],\n\nI hope you're doing well. I'm writing to request a meeting to discuss my compensation, in light of my contributions and performance over the past [time period].\n\nSince joining [or since my last review], I've:\n• [Achievement 1 with quantifiable result]\n• [Achievement 2 with quantifiable result]\n• [Achievement 3 - new responsibility taken on]\n• [Achievement 4]\n\nBased on my research of market rates for [role] in [location/industry], and considering my expanded responsibilities, I'd like to discuss adjusting my salary to [specific amount or range].\n\nWould you be available for a brief meeting this week to discuss? I'm open to your feedback and suggestions.\n\nThank you for your time and consideration.\n\nBest,\n[Your Name]\n\n---\n\nOption 2 - Conversation Script:\n'Hi [Manager], do you have a few minutes to chat? I'd like to discuss my role and compensation. I've been thinking about my contributions here—I've [achievement 1] and [achievement 2], and I've really grown into [new responsibility]. I'd love to talk about what growth looks like here, including a salary adjustment to reflect my expanded role.'\n\nTIPS:\n• Schedule the conversation, don't spring it\n• Come with specific achievements and numbers\n• Research market rates beforehand\n• Be confident but not demanding\n• Consider total compensation, not just salary"}}}}

70. User: "i need a doc for tracking my daily water intake"
Your response: {{"tool": "create_doc", "args": {{"title": "Daily Water Intake Tracker", "content": "DAILY WATER INTAKE TRACKER\n\nGOAL: ___ ounces/glasses per day (recommended: 8 glasses / 64 oz)\n\nMONDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nTUESDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nWEDNESDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nTHURSDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nFRIDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nSATURDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nSUNDAY:\n☐ Morning: ___ oz    ☐ Midday: ___ oz    ☐ Evening: ___ oz\nTotal: ___ oz    Goal Met? ☐ Yes ☐ No\n\nWEEKLY SUMMARY:\nDays goal met: ___ / 7\nTotal consumed: ___ oz\nAverage per day: ___ oz\n\nHYDRATION TIPS:\n• Drink a glass first thing in the morning\n• Keep a water bottle at your desk\n• Drink before every meal\n• Set hourly reminders\n• Infuse with lemon/cucumber for flavor"}}}}

71. User: "write a nice congrats message for my friend's promotion"
Your response: {{"tool": "create_doc", "args": {{"title": "Congratulations Messages", "content": "PROMOTION CONGRATULATIONS\n\nOption 1 - Warm & Simple:\nCongratulations on your promotion! 🎉 Your hard work and dedication have truly paid off. I'm so happy for you and can't wait to see all the amazing things you'll accomplish in your new role. Well deserved!\n\n---\n\nOption 2 - Detailed & Personal:\nHey [Name]!\n\nI just heard the amazing news about your promotion to [New Title]—congratulations! 🎊\n\nHaving worked with you [or: Knowing you as I do], I know how much you've put into this. The long hours, the creative problem-solving, the way you always go above and beyond—it hasn't gone unnoticed, and I'm thrilled that leadership recognized what I've known all along: you're a total rockstar.\n\nThis is just the beginning. Your new team is lucky to have you leading them.\n\nLet's celebrate soon! Drinks/dinner are on me. 🥂\n\nSo proud of you,\n[Your Name]\n\n---\n\nOption 3 - Short & Sweet:\nYES! So deserved! 👏 Congratulations on the promotion—you're going to crush it!\n\n---\n\nOption 4 - Funny:\nLook at you, climbing that corporate ladder! 🪜 Next stop: world domination? Seriously though, huge congrats on the promotion—you've earned every bit of it!"}}}}

72. User: "can you help me plan a weekend itinerary for nyc?"
Your response: {{"tool": "create_doc", "args": {{"title": "NYC Weekend Itinerary", "content": "NYC WEEKEND GETAWAY - ITINERARY\n\nSATURDAY:\n\nMorning:\n9:00 AM - Brunch at [Restaurant Name - e.g., Buvette, Sadelle's]\n10:30 AM - Walk through Central Park\n         • Bethesda Terrace\n         • The Mall\n         • Bow Bridge\n\nAfternoon:\n12:30 PM - Explore Metropolitan Museum of Art (The Met)\n         OR\n         Shopping in SoHo (Broadway, Prince, Spring Streets)\n3:00 PM - Coffee break at [local café]\n4:00 PM - Walk the High Line (Chelsea)\n         • Art installations\n         • Views of Hudson River\n\nEvening:\n6:00 PM - Dinner in West Village or Chelsea Market\n8:00 PM - Broadway show OR live jazz in Greenwich Village\n10:00 PM - Drinks at a rooftop bar (The Roof, Westlight)\n\nSUNDAY:\n\nMorning:\n9:00 AM - Breakfast bagel at [Ess-a-Bagel, Russ & Daughters]\n10:00 AM - Walk Brooklyn Bridge\n         • Photo ops of Manhattan skyline\n11:30 AM - Explore DUMBO (Down Under Manhattan Bridge Overpass)\n         • Washington Street photo spot\n         • Brooklyn Bridge Park\n\nAfternoon:\n1:00 PM - Lunch in Williamsburg, Brooklyn\n         • Trendy cafes and shops\n2:30 PM - Ferry back to Manhattan (scenic views!)\n3:30 PM - Grand Central Terminal (admire the architecture)\n4:30 PM - Final stroll through Times Square\n         OR\n         Last-minute shopping at Macy's/Herald Square\n\nEvening:\n6:00 PM - Farewell dinner\n         [Choose: Italian in Little Italy, Dim Sum in Chinatown, or Steakhouse]\n\nBUDGET ESTIMATE:\n• Meals: $____\n• Activities: $____\n• Transport: $____\n• Shopping: $____\n\nTIPS:\n• Wear comfortable walking shoes\n• Get a MetroCard or use OMNY\n• Book show tickets in advance\n• Check weather before finalizing"}}}}

73. User: "i need a template for tracking my job applications"
Your response: {{"tool": "create_doc", "args": {{"title": "Job Application Tracker", "content": "JOB APPLICATION TRACKER\n\nAPPLICATIONS:\n\n1. [Company Name] - [Role Title]\n   Date Applied: ___\n   Portal/Email: ___\n   Status: ☐ Applied ☐ Phone Screen ☐ Interview ☐ Offer ☐ Rejected\n   Follow-up Date: ___\n   Notes: _________________________\n\n2. [Company Name] - [Role Title]\n   Date Applied: ___\n   Portal/Email: ___\n   Status: ☐ Applied ☐ Phone Screen ☐ Interview ☐ Offer ☐ Rejected\n   Follow-up Date: ___\n   Notes: _________________________\n\n3. [Company Name] - [Role Title]\n   Date Applied: ___\n   Portal/Email: ___\n   Status: ☐ Applied ☐ Phone Screen ☐ Interview ☐ Offer ☐ Rejected\n   Follow-up Date: ___\n   Notes: _________________________\n\n4. [Company Name] - [Role Title]\n   Date Applied: ___\n   Portal/Email: ___\n   Status: ☐ Applied ☐ Phone Screen ☐ Interview ☐ Offer ☐ Rejected\n   Follow-up Date: ___\n   Notes: _________________________\n\n5. [Company Name] - [Role Title]\n   Date Applied: ___\n   Portal/Email: ___\n   Status: ☐ Applied ☐ Phone Screen ☐ Interview ☐ Offer ☐ Rejected\n   Follow-up Date: ___\n   Notes: _________________________\n\nSUMMARY STATS:\nTotal Applied: ___\nPhone Screens: ___\nInterviews: ___\nOffers: ___\n\nACTIVE FOLLOW-UPS NEEDED:\n• [Company] - Follow up on: [Date]\n• [Company] - Follow up on: [Date]\n\nINTERVIEW PREP NOTES:\nCommon questions to prepare:\n• Tell me about yourself\n• Why this company?\n• Strengths and weaknesses\n• Behavioral examples (STAR method)\n• Questions to ask them"}}}}

74. User: "write a farewell message for my coworker who's leaving"
Your response: {{"tool": "create_doc", "args": {{"title": "Farewell Messages for Coworker", "content": "FAREWELL MESSAGES FOR COWORKER\n\nOption 1 - Warm & Professional:\nDear [Name],\n\nAs you embark on this new chapter, I wanted to take a moment to say how much I've enjoyed working with you. Your [specific quality - e.g., 'attention to detail', 'positive attitude', 'creative problem-solving'] has made a real impact on our team, and you've set a high bar for all of us.\n\nWhile we'll definitely miss you around here, I'm excited for what lies ahead for you. Your new team is incredibly lucky to have you!\n\nLet's stay in touch—lunch is on me next time!\n\nWishing you all the best,\n[Your Name]\n\n---\n\nOption 2 - Personal & Friendly:\n[Name]!\n\nCan't believe you're leaving us! 😢 Working with you has been one of the best parts of my time here. From [specific memory - e.g., 'those late nights on the Johnson project'] to [another memory - e.g., 'our coffee runs'], you've made work feel less like... well, work.\n\nI'm genuinely sad to see you go, but I know you're going to absolutely crush it at [new company/in new role]. They have no idea how awesome they're getting!\n\nDon't forget us little people when you're famous. 😉\n\nKeep in touch!\n[Your Name]\n\n---\n\nOption 3 - Short for Card:\nWishing you the best on your next adventure! Thanks for being an amazing colleague and friend. You'll be missed! 🎉\n\n---\n\nOption 4 - Humorous:\nSo you're abandoning us, huh? Just kidding! 😄 Seriously though, it's been great working with you. Try not to make us look too bad at your new place! Good luck!"}}

75. User: "can you create a morning routine checklist?"
Your response: {{"tool": "create_doc", "args": {{"title": "Morning Routine Checklist", "content": "MY MORNING ROUTINE\n\n☀️ IMMEDIATE (Within 5 min of waking):\n☐ Don't hit snooze\n☐ Drink a full glass of water\n☐ Open curtains/blinds for natural light\n☐ Make the bed\n\n🧘 MINDFULNESS (5-10 min):\n☐ Stretch or light yoga\n☐ 5 minutes of meditation/breathing\n☐ Set intention for the day\n☐ Quick gratitude reflection (3 things)\n\n🚿 HYGIENE & SELF-CARE (15-20 min):\n☐ Shower or wash face\n☐ Skincare routine\n☐ Brush teeth\n☐ Get dressed\n☐ Style hair\n\n🍳 NOURISHMENT (15-20 min):\n☐ Healthy breakfast\n☐ Coffee or tea\n☐ Take vitamins/medication\n☐ Prep lunch (if taking to work)\n\n📋 PREPARATION (10-15 min):\n☐ Review calendar/today's schedule\n☐ Check and prioritize to-do list\n☐ Quick tidy (dishes, clutter)\n☐ Pack bag with essentials\n☐ Keys, wallet, phone check\n\n🚶 DEPARTURE:\n☐ Leave with time to spare\n☐ Podcast/music for commute\n☐ Positive mindset!\n\nROUTINE START TIME: _______\nTARGET END/DEPARTURE: _______\n\nNOTES & REFLECTION:\nWhat works: ___________________\nWhat to adjust: ___________________\nHow I feel after: ___________________"}}}}

EXAMPLES - EMAIL CONTENT IN DOCS (content topic ≠ mode, these are ALL docs requests):
76. User: "create a doc with content of 100 words of email draft to my employee asking for paid leave"
Your response: {{"tool": "create_doc", "args": {{"title": "Paid Leave Request Email Draft", "content": "Subject: Paid Leave Request\n\nDear [Employee Name],\n\nI am writing to formally request paid leave from [Start Date] to [End Date], totaling [X] working days. I plan to use this time for personal commitments.\n\nI will ensure all urgent tasks are completed before my departure and will prepare a detailed handover document for my responsibilities. I will coordinate with [Colleague Name] to cover any critical duties during my absence.\n\nPlease let me know if these dates work for the team, or if you need any additional information from me.\n\nThank you for your consideration.\n\nBest regards,\n[Your Name]"}}}}

77. User: "create a doc with an email to my boss asking for a raise"
Your response: {{"tool": "create_doc", "args": {{"title": "Salary Raise Request Email", "content": "Subject: Request for Salary Review\n\nDear [Manager's Name],\n\nI hope this message finds you well. I am writing to request a meeting to discuss my compensation.\n\nOver the past [X] months/years, I have [key achievement 1], [key achievement 2], and [key achievement 3]. I believe my contributions have significantly impacted [team/project/company goal].\n\nBased on my research and current market rates for my role, I would like to discuss adjusting my salary to [desired amount or range].\n\nI would welcome the opportunity to meet at your convenience to discuss this further.\n\nThank you for your time and consideration.\n\nSincerely,\n[Your Name]"}}}}

78. User: "create a doc for ot" (ot = overtime, just generate a relevant document)
Your response: {{"tool": "create_doc", "args": {{"title": "Overtime Request", "content": "OVERTIME REQUEST FORM\n\nEmployee Name: ___________________\nDepartment: ___________________\nDate: ___________________\nManager: ___________________\n\nOVERTIME DETAILS:\nDate(s) of OT: ___________________\nExpected Hours: ___________________\nProject/Reason: ___________________\n\nJUSTIFICATION:\n___________________________________________________\n___________________________________________________\n\nEmployee Signature: ___________________  Date: _______\nManager Approval: ___________________   Date: _______\nHR Acknowledgment: ___________________  Date: _______"}}}}

79. User: "make a doc with a cover letter for a software engineer job"
Your response: {{"tool": "create_doc", "args": {{"title": "Software Engineer Cover Letter", "content": "Dear Hiring Manager,\n\nI am excited to apply for the Software Engineer position at [Company Name]. With [X] years of experience in software development and a strong background in [relevant technologies], I am confident in my ability to contribute meaningfully to your team.\n\nIn my current role at [Current Company], I have [specific achievement, e.g., 'designed and deployed a microservices architecture that reduced system latency by 40%']. I thrive in collaborative environments and am passionate about writing clean, maintainable code.\n\nWhat excites me most about [Company Name] is [specific reason - culture, product, mission]. I am eager to bring my skills in [Skill 1], [Skill 2], and [Skill 3] to help achieve your goals.\n\nI would welcome the chance to discuss how my background aligns with your needs. Thank you for considering my application.\n\nSincerely,\n[Your Name]\n[Phone] | [Email] | [LinkedIn]"}}}}

"""
# Google Doc IDs: base64url, typically 44 chars (alphanumeric + _ + -)
_DOC_ID_RE = re.compile(r'[a-zA-Z0-9_-]{25,}')

def _extract_doc_id(text: str) -> Optional[str]:
    """Pull a Google Doc ID from a URL or standalone string."""
    # From URL: /document/d/DOC_ID/
    url_m = re.search(r'/document/d/([a-zA-Z0-9_-]{25,})', text)
    if url_m:
        return url_m.group(1)
    m = _DOC_ID_RE.search(text)
    return m.group(0) if m else None


def _fmt_docs(res_data: dict) -> str:
    """Render Docs tool results as HTML."""
    if not isinstance(res_data, dict) or "success" not in res_data:
        return str(res_data)
    if not res_data.get("success"):
        return f"<b style='color:#ef4444'>Error:</b> {res_data.get('error', 'Unknown error')}"

    res = res_data.get("result", "")

    def _render(data) -> str:
        if isinstance(data, list):
            if not data:
                return "No documents found."
            items = []
            for doc in data:
                if isinstance(doc, dict):
                    url   = doc.get("url", "")
                    title = doc.get("title", "(Untitled)")
                    mod   = doc.get("modified", "")[:10] if doc.get("modified") else ""
                    link  = f"<a href='{url}' target='_blank' style='color:var(--primary)'>{title}</a>" if url else f"<b>{title}</b>"
                    items.append(
                        f"<div style='padding:8px 10px;margin:5px 0;background:rgba(99,102,241,.08);"
                        f"border-left:3px solid var(--primary);border-radius:4px'>"
                        f"{link}<br>"
                        f"<span style='color:var(--text-muted);font-size:12px'>ID: {doc.get('id','')}"
                        f"{' · ' + mod if mod else ''}</span></div>"
                    )
                else:
                    items.append(str(doc))
            return "".join(items)
        if isinstance(data, dict):
            if "text" in data and "title" in data:
                url   = data.get("url", "")
                title = data.get("title", "(Untitled)")
                link  = f"<a href='{url}' target='_blank' style='color:var(--primary)'>{title}</a>" if url else f"<b>{title}</b>"
                text_preview = data["text"][:600].replace("\n", "<br>") if data["text"] else "(empty)"
                return (
                    f"<b>Document:</b> {link}<br>"
                    f"<span style='color:var(--text-muted);font-size:12px'>ID: {data.get('id','')}</span><br><br>"
                    f"<div style='font-size:13px;border-top:1px solid rgba(255,255,255,0.08);padding-top:8px'>"
                    f"{text_preview}</div>"
                )
            lines = [f"<b>{k.replace('_',' ').capitalize()}:</b> {_render(v)}"
                     for k, v in data.items() if v not in (None, "", [], {})]
            return "<br>".join(lines)
        return str(data).replace("\n", "<br>")

    if isinstance(res, str):
        return res.replace("\n", "<br>")
    return _render(res)


# ─────────────────────────────────────────────────────────────────────────────
# Google Docs intent router  (zero-latency fast path)
# ─────────────────────────────────────────────────────────────────────────────

def _docs_intent_detect(text: str, mcp, state: dict) -> Optional[str]:
    """Return an HTML reply string, or None to fall through to the LLM."""
    low  = text.lower().strip()
    dids = state.get("last_viewed_doc_ids", [])
    
    def _exec_tool_safe(tool_name: str, args: dict, success_msg: str) -> str:
        """Execute tool with proper error handling."""
        try:
            res = mcp.execute_tool(tool_name, args)
            if isinstance(res, dict) and res.get("success"):
                return success_msg + "<br><br>" + _fmt_docs(res)
            elif isinstance(res, dict) and not res.get("success"):
                err = res.get("error", "Unknown error")
                return f"<b style='color:#ef4444'>❌ Failed:</b> {err}"
            return success_msg + "<br><br>" + _fmt_docs(res)
        except PermissionError as e:
            return f"<b style='color:#ef4444'>🔒 Authentication Required:</b> {str(e)}"
        except Exception as e:
            return f"<b style='color:#ef4444'>⚠️ Error:</b> {str(e)}"

    # ── Greetings ────────────────────────────────────────────────────────────
    if re.match(r'^(hi+|hello+|hey+|howdy|greetings?)[\s!?.]*$', low):
        return (
            "👋 <b>Hi! I'm G-Assistant in Google Docs mode.</b><br><br>"
            "Here's what I can do:<br>"
            "• <b>List</b> your recent documents<br>"
            "• <b>Search</b> docs by title or content<br>"
            "• <b>Read</b> any document<br>"
            "• <b>Create</b> a new document<br>"
            "• <b>Append</b> text to an existing document<br>"
            "• <b>Find & Replace</b> text in a document<br>"
            "• <b>Rename</b> or <b>Delete</b> documents<br><br>"
            "Just tell me what you need!"
        )

    # ── List recent docs ──────────────────────────────────────────────────────
    if re.search(
        r'\b(?:list|show|get|fetch|display|see|view)\s+(?:my\s+|all\s+|recent\s+)?'
        r'(?:google\s+)?docs?(?:uments?)?\b', low
    ) or low in {"docs", "documents", "my docs", "recent docs"}:
        res = mcp.execute_tool("list_docs", {"limit": 10})
        ids = [d["id"] for d in (res.get("result") or []) if isinstance(d, dict)]
        state["last_viewed_doc_ids"] = ids
        return f"Your recent Google Docs:<br><br>{_fmt_docs(res)}"

    # ── Search docs ───────────────────────────────────────────────────────────
    m = re.search(
        r'\b(?:search|find|look\s+for)\s+(?:(?:google\s+)?docs?\s+)?'
        r'(?:about|with|containing|for|titled?|named?|regarding)?\s+["\']?(.+?)["\']?\s*$', low
    )
    if m:
        q = m.group(1).strip()
        if len(q) > 1 and q not in ("doc", "docs", "document", "documents"):
            res = mcp.execute_tool("search_docs", {"query": q, "limit": 10})
            ids = [d["id"] for d in (res.get("result") or []) if isinstance(d, dict)]
            state["last_viewed_doc_ids"] = ids
            return f"Search results for <b>{q}</b>:<br><br>{_fmt_docs(res)}"

    # ── Read / open a doc ─────────────────────────────────────────────────────
    m = re.search(
        r'\b(?:open|read|show|view|get|fetch|display)\s+(?:doc(?:ument)?\s+)?'
        r'([a-zA-Z0-9_-]{25,})\b', text
    )
    if not m:
        # URL form
        m = re.search(r'/document/d/([a-zA-Z0-9_-]{25,})', text)
    if m:
        doc_id = m.group(1)
        res    = mcp.execute_tool("get_doc", {"doc_id": doc_id})
        state["last_viewed_doc_ids"] = [doc_id]
        return f"Document contents:<br><br>{_fmt_docs(res)}"

    # ── Create a new doc ──────────────────────────────────────────────────────
    # Pattern 1: "create doc WITH content..." / "create doc with content as..." (content description - let LLM handle)
    content_pattern = re.search(
        r'\b(?:create|make|write)\s+(?:a\s+)?(?:new\s+)?(?:google\s+)?doc(?:ument)?'
        r'(?:\s+(?:with|containing|about|for))(?:\s+(?:the|content|text))?(?:\s+(?:as|of|like|about))?\s+(.+)', low, re.DOTALL
    )
    # Pattern 2: "create doc CALLED/NAMED/TITLED ..." (just title, no content)
    title_pattern = re.search(
        r'\b(?:create|make|new|write)\s+(?:a\s+)?(?:new\s+)?(?:google\s+)?doc(?:ument)?'
        r'(?:\s+(?:called|named|titled?))\s+["\']?(.+?)["\']?\s*$', low
    )
    # Pattern 3: Generic "create doc about X" - treat as content generation
    about_pattern = re.search(
        r'\b(?:create|make|write)\s+(?:a\s+)?(?:new\s+)?(?:google\s+)?doc(?:ument)?'
        r'\s+(?:about|on|for)\s+(.+)', low, re.DOTALL
    )
    
    if title_pattern and not content_pattern and not about_pattern:
        # Simple title-only creation - fast path
        title = title_pattern.group(1).strip().title()
        return _exec_tool_safe(
            "create_doc", 
            {"title": title, "content": ""},
            f"📄 Empty document '<b>{title}</b>' created."
        )
    # If content_pattern or about_pattern matches, fall through to LLM for content generation

    # ── Append text to a doc by ID ───────────────────────────────────────────
    m = re.search(
        r'\b(?:append|add|insert|write)\s+(?:to\s+)?(?:doc(?:ument)?\s+)?'
        r'([a-zA-Z0-9_-]{25,})\s*[:\-\u2013]?\s*(.*)', text, re.DOTALL
    )
    if m:
        doc_id = m.group(1)
        text_to_add = m.group(2).strip()
        # If no text provided or looks like a content description, fall to LLM
        if len(text_to_add) < 3 or re.search(r'\b(?:some|a|the|content|text|about|regarding)\b', text_to_add.lower()):
            return None  # Let LLM generate proper content
        return _exec_tool_safe(
            "append_to_doc", 
            {"doc_id": doc_id, "text": text_to_add},
            "✅ Text appended to document!"
        )
    
    # ── Append to last viewed doc (contextual) ────────────────────────────────
    if dids and re.search(
        r'\b(?:append|add|insert|write)\s+(?:to\s+)?(?:it|that|this|the\s+last\s+(?:doc|one))\s*[:\-\u2013]?\s*(.+)',
        low, re.DOTALL
    ):
        text_to_add = re.search(r'[:\-\u2013]?\s*(.+)', low, re.DOTALL)
        if text_to_add:
            content = text_to_add.group(1).strip()
            if len(content) > 5:
                return _exec_tool_safe(
                    "append_to_doc",
                    {"doc_id": dids[0], "text": content},
                    "✅ Text appended to the last document!"
                )

    # ── Rename a doc by ID ───────────────────────────────────────────────────
    m = re.search(
        r'\b(?:rename|retitle)\s+(?:doc(?:ument)?\s+)?([a-zA-Z0-9_-]{25,})\s+'
        r'(?:to|as)\s+["\']?(.+?)["\']?\s*$', low
    )
    if m:
        doc_id    = m.group(1)
        new_title = m.group(2).strip()
        return _exec_tool_safe(
            "update_doc_title", 
            {"doc_id": doc_id, "new_title": new_title},
            f"✏️ Document renamed to '<b>{new_title}</b>'"
        )
    
    # ── Rename last viewed doc (contextual) ──────────────────────────────────
    if dids and re.search(
        r'\b(?:rename|retitle)\s+(?:it|that|this|the\s+last\s+(?:doc|one))\s+(?:to|as)\s+["\']?(.+?)["\']?\s*$',
        low
    ):
        m = re.search(r'(?:to|as)\s+["\']?(.+?)["\']?\s*$', low)
        if m:
            new_title = m.group(1).strip()
            return _exec_tool_safe(
                "update_doc_title",
                {"doc_id": dids[0], "new_title": new_title},
                f"✏️ Last document renamed to '<b>{new_title}</b>'"
            )

    # ── Delete / trash a doc by ID ───────────────────────────────────────────
    m = re.search(
        r'\b(?:delete|trash|remove)\s+(?:doc(?:ument)?\s+)?([a-zA-Z0-9_-]{25,})\b', text
    )
    if m:
        doc_id = m.group(1)
        return _exec_tool_safe(
            "delete_doc", 
            {"doc_id": doc_id},
            "🗑️ Document moved to trash"
        )
    
    # ── Delete last viewed doc (contextual, with confirmation pattern) ─────────
    if dids and re.search(
        r'\b(?:delete|trash|remove)\s+(?:it|that|this|the\s+last\s+(?:doc|one))\b', low
    ):
        # Return a message asking for explicit ID (safer)
        return (
            f"⚠️ <b>Please confirm deletion</b><br><br>"
            f"To delete the last viewed document, please use its ID:<br>"
            f"<code>delete doc {dids[0]}</code>"
        )

    # ── "Open last / show it" — contextual ───────────────────────────────────
    if re.search(r'\b(?:open|read|show)\s+(?:it|that|this|the\s+(?:last|same)\s+one)\b', low):
        if dids:
            return _exec_tool_safe(
                "get_doc", 
                {"doc_id": dids[0]},
                "📄 Document contents:"
            )
        return "<i>No recent document to show. Try listing your docs first.</i>"
    
    # ── Find and Replace ─────────────────────────────────────────────────────
    replace_match = re.search(
        r'\b(?:find\s+(?:and\s+)?replace|replace)\s+(?:in\s+)?(?:doc(?:ument)?\s+)?'
        r'([a-zA-Z0-9_-]{25,})\s+["\']?(.+?)["\']?\s+(?:with|→|->|to)\s+["\']?(.+?)["\']?\s*$',
        low, re.DOTALL
    )
    if replace_match:
        doc_id = replace_match.group(1)
        find_text = replace_match.group(2).strip()
        replace_text = replace_match.group(3).strip()
        return _exec_tool_safe(
            "replace_text_in_doc",
            {"doc_id": doc_id, "find": find_text, "replace": replace_text},
            f"✅ Replaced '<b>{find_text}</b>' with '<b>{replace_text}</b>'"
        )
    
    # ── Get document content only ────────────────────────────────────────────
    m = re.search(
        r'\b(?:get|extract|pull|copy)\s+(?:content|text|body)\s+(?:from|of)\s+(?:doc(?:ument)?\s+)?'
        r'([a-zA-Z0-9_-]{25,})\b', low
    )
    if m:
        doc_id = m.group(1)
        return _exec_tool_safe(
            "get_doc_content",
            {"doc_id": doc_id},
            "📄 Document text:"
        )

    return None  # fall through to LLM


_SHEETS_SYSTEM_PROMPT = """\
You are G-Assistant operating in Google Sheets MCP mode.

Context:
- Date: {current_date}
- Recent spreadsheet IDs: {viewed_sheet_ids}

YOUR JOB: Convert the user's request into a single JSON tool call for Google Sheets.
Output ONLY the JSON block — no text before or after it.

```json
{{"tool": "TOOL_NAME", "args": {{"key": "value"}}}}
```

INFORMATION EXTRACTION:
- SPREADSHEET ID: Extract from URLs (string between /spreadsheets/d/ and /edit), or use recent IDs above.
- RANGE: Use A1 notation like "Sheet1!A1:D10", "Sheet1!A:A", or just "Sheet1" for the whole sheet.
- VALUES: Must be a 2-D JSON array — rows are inner arrays:
    e.g. [["Name","Age"],["Alice",30]] for a 2-row, 2-column block.
- CONTEXT REFERENCES: "it"/"that"/"the sheet" → use first ID from Recent spreadsheet IDs.

AVAILABLE TOOLS:
{tool_list}

TOOL GUIDE:
- list_sheets(limit)                              → list recent spreadsheets
- search_sheets(query, limit)                     → full-text search across spreadsheets
- get_sheet(sheet_id)                             → read spreadsheet metadata & tab names
- read_sheet(sheet_id, range_name)                → read cell values from a range
- create_sheet(title)                             → create a new spreadsheet
- write_to_sheet(sheet_id, range_name, values)    → overwrite a range with 2-D values
- append_to_sheet(sheet_id, range_name, values)   → append rows after existing data
- clear_sheet_range(sheet_id, range_name)         → clear values in a range
- add_sheet_tab(sheet_id, tab_name)               → add a new tab
- rename_sheet_tab(sheet_id, old_name, new_name)  → rename a tab
- delete_sheet(sheet_id)                          → move spreadsheet to trash

RULES:
1. ALWAYS attempt a tool call for any Sheets-related request. Never refuse.
2. When the user says "write Name, Age header" → values = [["Name","Age"]].
3. Only redirect for completely unrelated topics:
   "I'm in **Google Sheets MCP** mode. Switch to **General Assistant** for non-Sheets questions."
"""

# Spreadsheet IDs share the same format as Doc IDs
_SHEET_ID_RE = re.compile(r'[a-zA-Z0-9_-]{25,}')


def _extract_sheet_id(text: str) -> Optional[str]:
    url_m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]{25,})', text)
    if url_m:
        return url_m.group(1)
    m = _SHEET_ID_RE.search(text)
    return m.group(0) if m else None


def _fmt_sheets(res_data: dict) -> str:
    """Render Sheets tool results as HTML."""
    if not isinstance(res_data, dict) or "success" not in res_data:
        return str(res_data)
    if not res_data.get("success"):
        return f"<b style='color:#ef4444'>Error:</b> {res_data.get('error', 'Unknown error')}"

    res = res_data.get("result", "")

    def _render_table(values: list) -> str:
        if not values:
            return "(empty)"
        capped = values[:50]
        rows_html = []
        for i, row in enumerate(capped):
            tag   = "th" if i == 0 else "td"
            cells = "".join(
                f"<{tag} style='padding:4px 8px;border:1px solid rgba(255,255,255,0.1);"
                f"background:{'rgba(99,102,241,0.15)' if i==0 else 'transparent'}'>"
                f"{str(c)}</{tag}>"
                for c in row
            )
            rows_html.append(f"<tr>{cells}</tr>")
        truncation = (
            f"<tr><td colspan='99' style='color:var(--text-muted);font-size:12px;padding:4px 8px'>"
            f"…{len(values)-50} more rows</td></tr>"
        ) if len(values) > 50 else ""
        return (
            f"<div style='overflow-x:auto;margin-top:8px'>"
            f"<table style='border-collapse:collapse;font-size:13px'>"
            f"{''.join(rows_html)}{truncation}</table></div>"
        )

    def _render(data) -> str:
        if isinstance(data, list):
            # List of spreadsheet dicts (from list_sheets / search_sheets)
            if data and isinstance(data[0], dict) and "id" in data[0] and "title" in data[0]:
                items = []
                for s in data:
                    url   = s.get("url", "")
                    title = s.get("title", "(Untitled)")
                    mod   = s.get("modified", "")
                    link  = f"<a href='{url}' target='_blank' style='color:var(--primary)'>{title}</a>" if url else f"<b>{title}</b>"
                    items.append(
                        f"<div style='padding:8px 10px;margin:5px 0;background:rgba(99,102,241,.08);"
                        f"border-left:3px solid var(--primary);border-radius:4px'>"
                        f"{link}<br>"
                        f"<span style='color:var(--text-muted);font-size:12px'>ID: {s.get('id','')}"
                        f"{' · ' + mod if mod else ''}</span></div>"
                    )
                return "".join(items)
            # Raw 2-D values (from read_sheet result.values wrapped in list)
            return _render_table(data)
        if isinstance(data, dict):
            # read_sheet result
            if "values" in data:
                header = (
                    f"<b>Range:</b> {data.get('range','')}<br>"
                    f"<span style='color:var(--text-muted);font-size:12px'>"
                    f"{data.get('rows',0)} rows × {data.get('cols',0)} cols</span>"
                )
                return header + "<br>" + _render_table(data["values"])
            # get_sheet metadata
            if "tabs" in data:
                url   = data.get("url", "")
                title = data.get("title", "(Untitled)")
                link  = f"<a href='{url}' target='_blank' style='color:var(--primary)'>{title}</a>" if url else f"<b>{title}</b>"
                tabs_html = "".join(
                    f"<span style='display:inline-block;margin:2px 4px;padding:2px 8px;"
                    f"background:rgba(99,102,241,0.15);border-radius:4px;font-size:12px'>"
                    f"{t['name']} ({t['rows']}×{t['cols']})</span>"
                    for t in data["tabs"]
                )
                return (
                    f"<b>Spreadsheet:</b> {link}<br>"
                    f"<span style='color:var(--text-muted);font-size:12px'>ID: {data.get('id','')}</span><br>"
                    f"<b>Tabs:</b> {tabs_html}"
                )
            # Generic dict (create/write/append results)
            lines = [f"<b>{k.replace('_',' ').capitalize()}:</b> {_render(v)}"
                     for k, v in data.items() if v not in (None, "", [], {})]
            return "<br>".join(lines)
        return str(data).replace("\n", "<br>")

    if isinstance(res, str):
        return res.replace("\n", "<br>")
    return _render(res)


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets intent router  (zero-latency fast path)
# ─────────────────────────────────────────────────────────────────────────────

def _sheets_intent_detect(text: str, mcp, state: dict) -> Optional[str]:
    """Return an HTML reply string, or None to fall through to the LLM."""
    low  = text.lower().strip()
    sids = state.get("last_viewed_sheet_ids", [])

    # ── Greetings ────────────────────────────────────────────────────────────
    if re.match(r'^(hi+|hello+|hey+|howdy|greetings?)[\s!?.]*$', low):
        return (
            "👋 <b>Hi! I'm G-Assistant in Google Sheets mode.</b><br><br>"
            "Here's what I can do:<br>"
            "• <b>List</b> your recent spreadsheets<br>"
            "• <b>Search</b> sheets by title or content<br>"
            "• <b>Read</b> any sheet or cell range<br>"
            "• <b>Create</b> a new spreadsheet<br>"
            "• <b>Write / Append</b> data to a sheet<br>"
            "• <b>Clear</b> a range<br>"
            "• <b>Add / Rename</b> tabs<br>"
            "• <b>Delete</b> a spreadsheet<br><br>"
            "Just tell me what you need!"
        )

    # ── List sheets ───────────────────────────────────────────────────────────
    if re.search(
        r'\b(?:list|show|get|fetch|display|see|view)\s+(?:my\s+|all\s+|recent\s+)?'
        r'(?:google\s+)?(?:sheets?|spreadsheets?)\b', low
    ) or low in {"sheets", "spreadsheets", "my sheets", "recent sheets"}:
        res = mcp.execute_tool("list_sheets", {"limit": 10})
        ids = [s["id"] for s in (res.get("result") or []) if isinstance(s, dict)]
        state["last_viewed_sheet_ids"] = ids
        return f"Your recent spreadsheets:<br><br>{_fmt_sheets(res)}"

    # ── Search sheets ─────────────────────────────────────────────────────────
    m = re.search(
        r'\b(?:search|find|look\s+for)\s+(?:(?:google\s+)?sheets?\s+|spreadsheets?\s+)?'
        r'(?:about|for|with|containing|titled?|named?|regarding)?\s+["\']?(.+?)["\']?\s*$', low
    )
    if m:
        q = m.group(1).strip()
        if len(q) > 1 and q not in ("sheet", "sheets", "spreadsheet", "spreadsheets"):
            res = mcp.execute_tool("search_sheets", {"query": q, "limit": 10})
            ids = [s["id"] for s in (res.get("result") or []) if isinstance(s, dict)]
            state["last_viewed_sheet_ids"] = ids
            return f"Search results for <b>{q}</b>:<br><br>{_fmt_sheets(res)}"

    # ── Open / get sheet metadata ─────────────────────────────────────────────
    m = re.search(
        r'\b(?:open|show|get|view|info(?:rmation)?|details?|metadata)\s+'
        r'(?:sheet\s+|spreadsheet\s+)?([a-zA-Z0-9_-]{25,})\b', text
    )
    if not m:
        m = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]{25,})', text)
    if m:
        sid = m.group(1)
        res = mcp.execute_tool("get_sheet", {"sheet_id": sid})
        state["last_viewed_sheet_ids"] = [sid]
        return f"Spreadsheet details:<br><br>{_fmt_sheets(res)}"

    # ── Read a range ──────────────────────────────────────────────────────────
    m = re.search(
        r'\bread\s+(?:sheet\s+)?([a-zA-Z0-9_-]{25,})'
        r'(?:\s+(?:range|tab|sheet)?)?\s+([A-Za-z][A-Za-z0-9!:$]+)', text
    )
    if m:
        sid        = m.group(1)
        range_name = m.group(2)
        res = mcp.execute_tool("read_sheet", {"sheet_id": sid, "range_name": range_name})
        state["last_viewed_sheet_ids"] = [sid]
        return f"Sheet data (<b>{range_name}</b>):<br><br>{_fmt_sheets(res)}"

    # ── Read whole sheet ──────────────────────────────────────────────────────
    m = re.search(
        r'\b(?:read|show|display|fetch)\s+(?:sheet\s+|spreadsheet\s+)?([a-zA-Z0-9_-]{25,})\s*$', text
    )
    if m:
        sid = m.group(1)
        res = mcp.execute_tool("read_sheet", {"sheet_id": sid, "range_name": "Sheet1"})
        state["last_viewed_sheet_ids"] = [sid]
        return f"Sheet data:<br><br>{_fmt_sheets(res)}"

    # ── Write column headers to last/specified sheet ──────────────────────────
    # Catches: "put/add/write column names id,name,email"
    if re.search(r'\b(?:put|add|write|set|insert)\s+(?:the\s+)?column\s+(?:names?|headers?)\b', low):
        col_m = re.search(
            r'(?:column\s+(?:names?|headers?)\s+(?:as\s+)?|columns?\s*[=:]\s*)'
            r'([a-zA-Z_][a-zA-Z0-9_ ]*(?:[,/|]\s*[a-zA-Z_][a-zA-Z0-9_ ]*)+)', low
        )
        if not col_m:
            # "as id, name, email" anywhere in message
            col_m = re.search(r'\bas\s+([a-zA-Z_][a-zA-Z0-9_ ]*(?:[,/|]\s*[a-zA-Z_][a-zA-Z0-9_ ]*)+)', low)
        sid = _extract_sheet_id(text) or (sids[0] if sids else None)
        if sid and col_m:
            columns = [c.strip() for c in re.split(r'[,/|]', col_m.group(1)) if c.strip()]
            res = mcp.execute_tool("write_to_sheet", {
                "sheet_id": sid, "range_name": "Sheet1!A1", "values": [columns]
            })
            state["last_viewed_sheet_ids"] = [sid]
            return f"✅ Headers <b>{', '.join(columns)}</b> written to the spreadsheet!<br><br>{_fmt_sheets(res)}"
        if sid and not col_m:
            return (
                "Please tell me the column names, e.g.<br>"
                "<b>add column headers: id, name, email</b>"
            )

    # ── Create a new spreadsheet (smart title + optional column headers) ──────
    if re.search(r'\b(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?(?:google\s+)?(?:sheet|spreadsheet)\b', low):
        # --- extract title ---
        title = None
        # "name this sheet as X" / "name it X" / "call it X"
        t_m = re.search(
            r'(?:name|call|title)\s+(?:this\s+)?(?:sheet|spreadsheet|it)\s+(?:as\s+)?["\']?([^"\']+?)["\']?'
            r'\s*(?:$|with\b|and\b|,)',
            low
        )
        if t_m:
            title = t_m.group(1).strip().title()
        if not title:
            # "called X" / "named X"
            t_m = re.search(r'(?:called|named|titled?)\s+["\']?([^"\']+?)["\']?\s*(?:$|with\b|and\b|,)', low)
            if t_m:
                title = t_m.group(1).strip().title()
        if not title:
            title = "Untitled Sheet"

        # --- extract column names ---
        columns = []
        col_m = re.search(
            r'column\s+(?:names?|headers?)\s+(?:as\s+)?([a-zA-Z_][a-zA-Z0-9_ ]*(?:[,/|]\s*[a-zA-Z_][a-zA-Z0-9_ ]*)+)',
            low
        )
        if col_m:
            columns = [c.strip() for c in re.split(r'[,/|]', col_m.group(1)) if c.strip()]

        # --- create the sheet ---
        res      = mcp.execute_tool("create_sheet", {"title": title})
        result   = res.get("result") if isinstance(res, dict) else {}
        sheet_id = result.get("id", "") if isinstance(result, dict) else ""
        if sheet_id:
            state["last_viewed_sheet_ids"] = [sheet_id]

        # --- write headers if columns were specified ---
        if columns and sheet_id:
            mcp.execute_tool("write_to_sheet", {
                "sheet_id": sheet_id,
                "range_name": "Sheet1!A1",
                "values": [columns]
            })
            return (
                f"📊 Spreadsheet <b>{title}</b> created with headers "
                f"<b>{', '.join(columns)}</b>!<br><br>{_fmt_sheets(res)}"
            )

        return f"📊 Spreadsheet <b>{title}</b> created!<br><br>{_fmt_sheets(res)}"

    # ── Add a tab ─────────────────────────────────────────────────────────────
    m = re.search(
        r'\badd\s+(?:a\s+)?(?:tab|sheet)\s+(?:called|named)?\s*["\']?([^"\']+?)["\']?'
        r'\s+(?:to|in)\s+([a-zA-Z0-9_-]{25,})', low
    )
    if m:
        tab_name = m.group(1).strip()
        sid      = m.group(2)
        res = mcp.execute_tool("add_sheet_tab", {"sheet_id": sid, "tab_name": tab_name})
        return f"✅ Tab <b>{tab_name}</b> added!<br><br>{_fmt_sheets(res)}"

    # ── Clear a range ─────────────────────────────────────────────────────────
    m = re.search(
        r'\bclear\s+(?:range\s+)?([A-Za-z][A-Za-z0-9!:$]+)\s+'
        r'(?:in|from|of)\s+([a-zA-Z0-9_-]{25,})', text
    )
    if not m:
        m = re.search(
            r'\bclear\s+([a-zA-Z0-9_-]{25,})\s+([A-Za-z][A-Za-z0-9!:$]+)', text
        )
    if m:
        range_name = m.group(1) if re.match(r'[A-Za-z]', m.group(1)) else m.group(2)
        sid        = m.group(2) if re.match(r'[A-Za-z]', m.group(1)) else m.group(1)
        res = mcp.execute_tool("clear_sheet_range", {"sheet_id": sid, "range_name": range_name})
        return f"🧹 Range cleared!<br><br>{_fmt_sheets(res)}"

    # ── Delete / trash a spreadsheet ──────────────────────────────────────────
    m = re.search(
        r'\b(?:delete|trash|remove)\s+(?:sheet\s+|spreadsheet\s+)?([a-zA-Z0-9_-]{25,})\b', text
    )
    if m:
        sid = m.group(1)
        res = mcp.execute_tool("delete_sheet", {"sheet_id": sid})
        return f"🗑️ Spreadsheet moved to trash.<br><br>{_fmt_sheets(res)}"

    # ── Contextual: open last sheet ───────────────────────────────────────────
    if re.search(r'\b(?:open|read|show)\s+(?:it|that|this|the\s+(?:last|same)\s+one)\b', low):
        if sids:
            res = mcp.execute_tool("get_sheet", {"sheet_id": sids[0]})
            return f"Spreadsheet details:<br><br>{_fmt_sheets(res)}"

    return None  # fall through to LLM


_MODE_NAMES: dict[str, str] = {
    "drive": "Google Drive MCP",
}


# ─────────────────────────────────────────────────────────────────────────────
# Attachment detection
# ─────────────────────────────────────────────────────────────────────────────

def _has_attachment(user_input: str, image_state: Optional[dict]) -> bool:
    return (
        image_state is not None
        or "--- Attached file:" in user_input
        or "[User attached image:" in user_input
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_agent(user_input: str, mcp, state: dict, mode: str = "gmail") -> str:
    """
    Route the user message to the correct handler and return an HTML reply string.

    Parameters
    ----------
    user_input : str
        Raw user message (may contain injected file content from web.py).
    mcp        : MCPServer
        Tool executor instance.
    state      : dict
        Mutable per-session state (history, last IDs, draft cache, etc.).
    mode       : str
        Active integration: "gmail" | "general" | "drive" | "docs" | "sheets".
    """
    now_str     = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    image_state = state.pop("last_image", None)      # consumed here; web.py sets this
    attachment  = _has_attachment(user_input, image_state)

    # ── Fast path: intent routers (bypassed when a file is attached) ─────────
    if mode == "gmail" and not attachment:
        intent_reply = _intent_detect(user_input, mcp, state)
        if intent_reply is not None:
            return intent_reply

    if mode == "docs" and not attachment:
        intent_reply = _docs_intent_detect(user_input, mcp, state)
        if intent_reply is not None:
            return intent_reply

    if mode == "sheets" and not attachment:
        intent_reply = _sheets_intent_detect(user_input, mcp, state)
        if intent_reply is not None:
            return intent_reply

    # ── Build mode-specific system prompt ────────────────────────────────────
    if mode == "gmail":
        tool_list  = "\n".join(f"- {k}" for k in GMAIL_TOOLS)
        v_ids      = state.get("last_viewed_ids", [])
        d_id       = state.get("last_draft_id", "None")
        sys_prompt = _GMAIL_SYSTEM_PROMPT.format(
            current_date=now_str,
            viewed_ids=", ".join(v_ids) if v_ids else "None",
            last_draft_id=d_id,
            last_to=state.get("last_to") or "None",
            tool_list=tool_list,
        )
    elif mode == "docs":
        tool_list  = "\n".join(f"- {k}" for k in DOCS_TOOLS)
        d_ids      = state.get("last_viewed_doc_ids", [])
        sys_prompt = _DOCS_SYSTEM_PROMPT.format(
            current_date=now_str,
            viewed_doc_ids=", ".join(d_ids) if d_ids else "None",
            tool_list=tool_list,
        )
    elif mode == "sheets":
        tool_list  = "\n".join(f"- {k}" for k in SHEETS_TOOLS)
        s_ids      = state.get("last_viewed_sheet_ids", [])
        sys_prompt = _SHEETS_SYSTEM_PROMPT.format(
            current_date=now_str,
            viewed_sheet_ids=", ".join(s_ids) if s_ids else "None",
            tool_list=tool_list,
        )
    elif mode == "general":
        sys_prompt = _GENERAL_SYSTEM_PROMPT.format(current_date=now_str)
    else:
        mode_name  = _MODE_NAMES.get(mode, mode.title() + " MCP")
        sys_prompt = (
            _ATTACHMENT_PROMPT.format(current_date=now_str, mode_name=mode_name)
            if attachment
            else _COMING_SOON_PROMPT.format(current_date=now_str, mode_name=mode_name)
        )

    # ── Per-mode conversation history ─────────────────────────────────────────
    hist_key = f"history_{mode}"
    if hist_key not in state:
        state[hist_key] = [{"role": "system", "content": sys_prompt}]
    else:
        state[hist_key][0] = {"role": "system", "content": sys_prompt}  # refresh context

    history = state[hist_key]

    # ── Build user message (vision block or plain text) ───────────────────────
    if image_state:
        vision_content = [
            {"type": "text", "text": user_input},
            {"type": "image_url", "image_url": {
                "url": f"data:{image_state['mime']};base64,{image_state['data']}"
            }}
        ]
        history.append({"role": "user", "content": vision_content})
    else:
        _append_to_history(history, "user", user_input)

    # ── Trim history to avoid context overflow ────────────────────────────────
    trimmed = _trim_history(history)
    if trimmed is not history:
        state[hist_key] = trimmed
        history = trimmed

    # ── LLM inference loop ────────────────────────────────────────────────────
    reply_text     = ""
    vision_slimmed = False
    _tool_refusals = 0  # count refusal retries to avoid infinite loops
    _REFUSAL_PHRASES = (
        "i apologize", "i cannot", "i'm unable", "unable to", "authentication",
        "permission", "scope", "since i cannot", "there was an issue",
        "not able to", "unfortunately", "i don't have access",
        "switch to general assistant", "switch to **general assistant",
        "non-docs questions", "google docs mcp mode",
    )

    for _ in range(MAX_TOOL_LOOPS):
        try:
            resp    = call_model(history)
            llm_msg = resp.get("choices", [{}])[0].get("message", {})
            content = llm_msg.get("content") or ""

            # Slim down vision entry after first successful call
            if image_state and not vision_slimmed:
                _slim_vision_entry(history, image_state["name"])
                vision_slimmed = True

            # Append assistant turn (truncated to avoid history bloat)
            _append_to_history(history, "assistant",
                               content if isinstance(content, str) else json.dumps(content))

            # Tool execution (Gmail, Docs, Sheets modes)
            if mode in ("gmail", "docs", "sheets") and isinstance(content, str):
                tool_call = _parse_tool_call(content)
                if tool_call:
                    tool_name = tool_call["tool"]
                    args      = tool_call.get("args", {})

                    # Gmail-specific: attach file if pending
                    if mode == "gmail" and state.get("last_attachment_path") \
                            and tool_name in ("send_email", "draft_email"):
                        args["attachment_path"] = state.pop("last_attachment_path")

                    res = mcp.execute_tool(tool_name, args)

                    # Gmail-specific: cache draft ID
                    if mode == "gmail" and tool_name == "draft_email" and isinstance(res, dict):
                        did = (res.get("result") or {}).get("id") if isinstance(res.get("result"), dict) else None
                        if did:
                            state["last_draft_id"] = did

                    # Docs-specific: cache last viewed doc IDs
                    if mode == "docs" and isinstance(res, dict):
                        result_data = res.get("result")
                        if isinstance(result_data, list):
                            ids = [d["id"] for d in result_data if isinstance(d, dict) and "id" in d]
                            if ids:
                                state["last_viewed_doc_ids"] = ids
                        elif isinstance(result_data, dict) and "id" in result_data:
                            state["last_viewed_doc_ids"] = [result_data["id"]]

                    # Sheets-specific: cache last viewed sheet IDs
                    if mode == "sheets" and isinstance(res, dict):
                        result_data = res.get("result")
                        if isinstance(result_data, list):
                            ids = [s["id"] for s in result_data if isinstance(s, dict) and "id" in s]
                            if ids:
                                state["last_viewed_sheet_ids"] = ids
                        elif isinstance(result_data, dict) and "id" in result_data:
                            state["last_viewed_sheet_ids"] = [result_data["id"]]

                    _append_to_history(history, "user", f"Tool result: {json.dumps(res)}")
                    continue  # next loop iteration → send tool result back to LLM

            # Guard: if docs/sheets mode returns plain text with a refusal instead of
            # a JSON tool call, re-inject a correction and retry (max 1 retry).
            if mode in ("docs", "sheets") and isinstance(content, str) and _tool_refusals < 1:
                lower = content.lower()
                if any(phrase in lower for phrase in _REFUSAL_PHRASES):
                    _tool_refusals += 1
                    _append_to_history(
                        history, "user",
                        'SYSTEM CORRECTION: Do NOT apologize or redirect. '
                        'You MUST output ONLY a JSON tool call like: '
                        '{"tool": "TOOL_NAME", "args": {"key": "value"}}. '
                        'Use single curly braces. Generate all content yourself and put it in the JSON. Try again now.'
                    )
                    continue

            reply_text = content if isinstance(content, str) else json.dumps(content)
            break

        except Exception:
            logger.exception("LLM call failed (mode=%s)", mode)
            reply_text = (
                "<b style='color:#ef4444'>Error:</b> The assistant encountered a problem. "
                "Please try again."
            )
            break

    # ── Fallback: format the most recent tool result if LLM loop exhausted ──────
    if not reply_text:
        _fmt_for_mode = (
            _fmt_docs   if mode == "docs"   else
            _fmt_sheets if mode == "sheets" else
            _fmt
        )
        # Search backwards through history for the most recent tool result
        # (LLM may have appended an empty/assistant turn after the tool result)
        for _msg in reversed(history):
            _c = _msg.get("content", "")
            if isinstance(_c, str) and "Tool result:" in _c:
                try:
                    _raw = _c.split("Tool result: ", 1)[1]
                    reply_text = _fmt_for_mode(json.loads(_raw))
                except (json.JSONDecodeError, ValueError, IndexError):
                    reply_text = _c
                break
        if not reply_text:
            reply_text = "I wasn't able to complete that. Please try rephrasing your request."

    return reply_text
