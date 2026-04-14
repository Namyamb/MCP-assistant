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

YOUR JOB: Convert the user's request into a single JSON tool call for Google Docs.
Output ONLY the JSON block — no text before or after it.

```json
{{"tool": "TOOL_NAME", "args": {{"key": "value"}}}}
```

INFORMATION EXTRACTION:
- DOCUMENT ID: Extract from URLs (the long string between /d/ and /edit), direct IDs, or use recent IDs above.
- TITLE: Use as-is from user request; "about X" → title = "X"
- CONTENT: When creating/appending, write full professional content based on the user's intent.
  e.g. "a meeting agenda for Monday" → write a proper formatted agenda

CONTEXT REFERENCES:
- "it" / "that doc" / "the document" / "this" → use first ID from Recent document IDs
- "the last one" / "same" → use first ID from Recent document IDs

AVAILABLE TOOLS:
{tool_list}

TOOL GUIDE:
- list_docs(limit)                         → list recent docs
- search_docs(query, limit)                → full-text search across all docs
- get_doc(doc_id)                          → read a document's full content
- get_doc_content(doc_id)                  → read just the text
- create_doc(title, content)               → create a new document
- append_to_doc(doc_id, text)              → add text at end of document
- replace_text_in_doc(doc_id, find, replace) → find & replace in document
- update_doc_title(doc_id, new_title)      → rename a document
- delete_doc(doc_id)                       → move document to trash

RULES:
1. ALWAYS attempt a tool call for any Docs-related request. Never refuse.
2. When content is a description, GENERATE proper content — don't echo the user's instruction.
3. Only redirect for completely unrelated topics (math, email, cooking):
   "I'm in **Google Docs MCP** mode. Switch to **General Assistant** for non-Docs questions."
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
    m = re.search(
        r'\b(?:create|make|new|write)\s+(?:a\s+)?(?:new\s+)?(?:google\s+)?doc(?:ument)?'
        r'(?:\s+(?:called|named|titled?|about|:))?\s+["\']?(.+?)["\']?\s*$', low
    )
    if m:
        title = m.group(1).strip().title()
        res   = mcp.execute_tool("create_doc", {"title": title, "content": ""})
        if isinstance(res.get("result"), dict):
            state["last_viewed_doc_ids"] = [res["result"].get("id", "")]
        return f"📄 Document created!<br><br>{_fmt_docs(res)}"

    # ── Append text to a doc ──────────────────────────────────────────────────
    m = re.search(
        r'\b(?:append|add|insert|write)\s+(?:to\s+)?(?:doc(?:ument)?\s+)?'
        r'([a-zA-Z0-9_-]{25,})\s*[:\-–]?\s*(.*)', text, re.DOTALL
    )
    if m:
        doc_id = m.group(1)
        text_to_add = m.group(2).strip() or "..."
        res = mcp.execute_tool("append_to_doc", {"doc_id": doc_id, "text": text_to_add})
        return f"✅ Text appended!<br><br>{_fmt_docs(res)}"

    # ── Rename a doc ──────────────────────────────────────────────────────────
    m = re.search(
        r'\b(?:rename|retitle)\s+(?:doc(?:ument)?\s+)?([a-zA-Z0-9_-]{25,})\s+'
        r'(?:to|as)\s+["\']?(.+?)["\']?\s*$', low
    )
    if m:
        doc_id    = m.group(1)
        new_title = m.group(2).strip()
        res = mcp.execute_tool("update_doc_title", {"doc_id": doc_id, "new_title": new_title})
        return f"✏️ Document renamed to <b>{new_title}</b>.<br><br>{_fmt_docs(res)}"

    # ── Delete / trash a doc ──────────────────────────────────────────────────
    m = re.search(
        r'\b(?:delete|trash|remove)\s+(?:doc(?:ument)?\s+)?([a-zA-Z0-9_-]{25,})\b', text
    )
    if m:
        doc_id = m.group(1)
        res    = mcp.execute_tool("delete_doc", {"doc_id": doc_id})
        return f"🗑️ Document moved to trash.<br><br>{_fmt_docs(res)}"

    # ── "Open last / show it" — contextual ───────────────────────────────────
    if re.search(r'\b(?:open|read|show)\s+(?:it|that|this|the\s+(?:last|same)\s+one)\b', low):
        if dids:
            res = mcp.execute_tool("get_doc", {"doc_id": dids[0]})
            return f"Document contents:<br><br>{_fmt_docs(res)}"

    return None  # fall through to LLM


_MODE_NAMES: dict[str, str] = {
    "drive":  "Google Drive MCP",
    "sheets": "Google Sheets MCP",
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
    reply_text    = ""
    vision_slimmed = False

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

            # Tool execution (Gmail and Docs modes)
            if mode in ("gmail", "docs") and isinstance(content, str):
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

                    _append_to_history(history, "user", f"Tool result: {json.dumps(res)}")
                    continue  # next loop iteration → send tool result back to LLM

            reply_text = content if isinstance(content, str) else json.dumps(content)
            break

        except Exception:
            logger.exception("LLM call failed (mode=%s)", mode)
            reply_text = (
                "<b style='color:#ef4444'>Error:</b> The assistant encountered a problem. "
                "Please try again."
            )
            break

    # ── Fallback: format the last tool result if LLM loop exhausted ──────────
    if not reply_text:
        last_content = history[-1].get("content", "") if history else ""
        if isinstance(last_content, str) and "Tool result:" in last_content:
            try:
                raw = last_content.split("Tool result: ", 1)[1]
                reply_text = _fmt(json.loads(raw))
            except (json.JSONDecodeError, ValueError, IndexError):
                reply_text = last_content
        else:
            reply_text = "I wasn't able to complete that. Please try rephrasing your request."

    return reply_text
