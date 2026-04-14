import json
import re
from app.core.config import MAX_TOOL_LOOPS

# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────
def _fmt(res_data):
    """Recursively pretty-print MCP tool results as HTML."""
    if not isinstance(res_data, dict) or "success" not in res_data:
        return str(res_data)
    if not res_data.get("success"):
        err = res_data.get("error", "Unknown error")
        return f"<b style='color:#ef4444'>Error:</b> {err}"

    res = res_data.get("result", "")

    def _recurse(data):
        if isinstance(data, list):
            if not data:
                return "No results found."
            blocks = []
            for item in data:
                blocks.append(
                    f"<div style='padding:10px;margin:6px 0;"
                    f"background:rgba(99,102,241,.1);border-left:3px solid "
                    f"var(--primary);border-radius:4px'>{_recurse(item)}</div>"
                )
            return "".join(blocks)
        elif isinstance(data, dict):
            if "subject" in data and "from" in data:
                sender = data.get("from", "").split("<")[0].strip()
                subj   = data.get("subject", "(No Subject)")
                snip   = data.get("snippet", "")
                date   = data.get("date", "")
                eid    = data.get("id", "")
                return (
                    f"<b>From:</b> {sender}<br>"
                    f"<b>Subject:</b> {subj}<br>"
                    f"<span style='color:var(--text-muted);font-size:13px'>{snip}</span><br>"
                    f"<span style='color:var(--text-muted);font-size:11px'>{date} · ID: {eid}</span>"
                )
            lines = []
            for k, v in data.items():
                if v not in (None, "", [], {}):
                    lines.append(f"<b>{str(k).replace('_',' ').capitalize()}:</b> {_recurse(v)}")
            return "<br>".join(lines)
        else:
            return str(data).replace("\n", "<br>")

    if isinstance(res, str):
        return res.replace("\n", "<br>")
    return _recurse(res)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
_EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}')

def _extract_email(text):
    m = _EMAIL_RE.search(text)
    return m.group(0) if m else None

def _is_real_recipient(word):
    """Return True only if `word` looks like a real email/username — not a common English word."""
    STOP_WORDS = {
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
    }
    if not word or len(word) < 2:
        return False
    if word.lower() in STOP_WORDS:
        return False
    # Must either contain @ (proper email) or look like a username/domain
    if "@" in word:
        return bool(_EMAIL_RE.match(word))
    # Accept only if it looks like a name/username (letters, dots, underscores, hyphens)
    if re.match(r'^[a-zA-Z][\w.\-]{2,}$', word):
        return True
    return False


# ──────────────────────────────────────────────
# Intent detection  (no LLM needed)
# ──────────────────────────────────────────────
def _intent_detect(text, mcp, state):
    """Return reply_text or None (fall through to LLM)."""
    low = text.lower().strip()

    # ── Greetings / capability questions ────────
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

    if re.search(r'\b(what can you do|what do you do|help me|your (features?|capabilities?|abilities?)|how (do i|can i) use)\b', low):
        return (
            "🤖 <b>G-Assistant capabilities:</b><br><br>"
            "<b>📬 Reading emails:</b><br>"
            "• <code>read my emails</code> · <code>show inbox</code> · <code>check unread</code> · <code>starred emails</code><br><br>"
            "<b>🔍 Searching:</b><br>"
            "• <code>find emails from john@example.com</code> · <code>search emails about invoice</code><br><br>"
            "<b>✉️ Sending / Drafting:</b><br>"
            "• <code>send email to john@example.com saying Hello!</code><br>"
            "• <code>draft email to alice@example.com saying Meeting tomorrow</code><br><br>"
            "<b>🗑️ Deleting / Archiving:</b><br>"
            "• <code>delete the latest email</code> · <code>trash email [ID]</code> · <code>archive email [ID]</code><br><br>"
            "<b>↩️ Replying / Forwarding:</b><br>"
            "• <code>reply to [ID] saying Thanks!</code> · <code>forward [ID] to bob@example.com</code><br><br>"
            "<b>📋 AI features:</b><br>"
            "• <code>summarize email [ID]</code> · <code>classify email [ID]</code><br><br>"
            "<b>🏷️ Labels:</b><br>"
            "• <code>list labels</code> · <code>create label work</code> · <code>add label work to [ID]</code>"
        )

    # ── Read latest / single email ───────────────
    if re.search(
        r'\b(?:get|fetch|show|give|retrieve|find|read)\s+(?:me\s+)?(?:the\s+)?'
        r'(?:latest|last|most\s+recent|newest|first|top|recent)\s+(?:email|mail|message)\b', low
    ):
        res = mcp.execute_tool("get_emails", {"limit": 1})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Here is your latest email:<br><br>{_fmt(res)}"

    # ── Read inbox (broad — many natural phrasings) ──
    if re.search(
        r'\b(?:read|show|check|open|get|display|list|fetch|give me|see)\s+'
        r'(?:me\s+)?(?:my\s+|all\s+|the\s+)?(?:inbox|emails?|mails?|messages?)\b', low
    ) or re.search(r'\b(?:my\s+emails?|my\s+inbox|my\s+mails?|my\s+messages?)\b', low) \
      or low in ("emails", "inbox", "mail", "messages"):
        res = mcp.execute_tool("get_emails", {"limit": 10})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Here are your latest emails:<br><br>{_fmt(res)}"

    # ── Unread emails ────────────────────────────
    if re.search(r'\b(?:unread|unseen)\s*(?:emails?|mails?|messages?)?\b', low) \
       or re.search(r'\bemails?\s+(?:i\s+)?(?:haven\'t\s+read|not\s+read)\b', low):
        res = mcp.execute_tool("get_unread_emails", {})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Your unread emails:<br><br>{_fmt(res)}"

    # ── Starred emails ───────────────────────────
    if re.search(r'\b(?:starred|important|flagged)\s*(?:emails?|mails?|messages?)?\b', low):
        res = mcp.execute_tool("get_starred_emails", {})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Your starred emails:<br><br>{_fmt(res)}"

    # ── Last week emails ─────────────────────────
    if re.search(r'\blast\s+week\b', low):
        from datetime import datetime, timedelta
        today = datetime.utcnow()
        start = (today - timedelta(days=7)).strftime("%Y/%m/%d")
        end   = today.strftime("%Y/%m/%d")
        res = mcp.execute_tool("get_emails_by_date_range", {"start": start, "end": end})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Emails from the last week:<br><br>{_fmt(res)}"

    # ── Today's emails ───────────────────────────
    if re.search(r'\btoday\b', low) and re.search(r'\b(?:email|mail|message)\b', low):
        from datetime import datetime
        today = datetime.utcnow().strftime("%Y/%m/%d")
        res = mcp.execute_tool("search_emails", {"query": f"after:{today}", "limit": 10})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Emails from today:<br><br>{_fmt(res)}"

    # ── Bulk delete all tracked emails ──────────
    if re.search(r'\b(?:delete|trash|remove)\s+(?:all|all\s+of\s+(?:these|them|those)|these|them|those)\b', low):
        ids = state.get("last_viewed_ids", [])
        if not ids:
            return "⚠️ I don't have any emails tracked. Try saying <b>'show my emails'</b> first, then ask me to delete them."
        ok, fail = 0, 0
        for mid in ids:
            res = mcp.execute_tool("trash_email", {"message_id": mid})
            if res.get("success") is not False:
                ok += 1
            else:
                fail += 1
        state["last_viewed_ids"] = []
        return f"🗑️ Moved <b>{ok}</b> email(s) to trash.{(' ⚠️ ' + str(fail) + ' failed.') if fail else ''}"

    # ── Delete latest / first / last one ────────
    m_pos = re.search(
        r'\b(?:delete|trash|remove)\s+(?:the\s+)?(?P<pos>first|last|latest|most\s+recent|newest)\s+'
        r'(?:one|mail|email|message)\b', low
    )
    if m_pos:
        ids = state.get("last_viewed_ids", [])
        if not ids:
            res = mcp.execute_tool("get_emails", {"limit": 1})
            ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
            state["last_viewed_ids"] = ids
        if not ids:
            return "⚠️ Couldn't retrieve any emails to delete."
        pos = m_pos.group("pos")
        target = ids[-1] if pos in ("last", "latest", "most recent", "newest") else ids[0]
        mcp.execute_tool("trash_email", {"message_id": target})
        return f"🗑️ Email <b>{target}</b> moved to trash."

    # ── Move / Trash latest ──────────────────────
    if re.search(
        r'\b(?:move|put|trash)\s+(?:it|this|the\s+(?:latest|last|first|email|mail|message))?\s*(?:to\s+)?trash\b', low
    ) or re.search(
        r'\bdelete\s+(?:the\s+)?(?:latest|last|most\s+recent)\s+(?:email|mail|message)\b', low
    ):
        ids = state.get("last_viewed_ids", [])
        if not ids:
            res = mcp.execute_tool("get_emails", {"limit": 1})
            ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
            state["last_viewed_ids"] = ids
        if not ids:
            return "⚠️ Couldn't fetch any emails to trash."
        target = ids[0]
        mcp.execute_tool("trash_email", {"message_id": target})
        state["last_viewed_ids"] = ids[1:]
        return f"🗑️ Moved email <b>{target}</b> to trash."

    # ── Delete / Trash / Archive / Star by explicit hex ID ──
    m = re.search(r'\b(?:delete|trash|remove)\s+(?:this\s+|it\s+|the\s+(?:email|mail)\s+)?([a-fA-F0-9]{10,})\b', low)
    if not m:
        m = re.search(r'\b(?:trash|delete)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        msg_id = m.group(1)
        res = mcp.execute_tool("trash_email", {"message_id": msg_id})
        return f"🗑️ Email <b>{msg_id}</b> moved to trash.<br><br>{_fmt(res)}"

    # "delete this" when an ID is visible nearby
    m = re.search(r'\b(?:delete|trash|remove)\s+(?:this|it)\b.*?([a-fA-F0-9]{10,})', low)
    if not m:
        m = re.search(r'([a-fA-F0-9]{10,}).*?\b(?:delete|trash|remove)\s+(?:this|it)\b', low)
    if m:
        msg_id = m.group(1)
        res = mcp.execute_tool("trash_email", {"message_id": msg_id})
        return f"🗑️ Done! Email <b>{msg_id}</b> moved to trash.<br><br>{_fmt(res)}"

    # ── Read / Show a specific email by ID ──────
    m = re.search(r'\b(?:show|read|open|view|get|fetch)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        msg_id = m.group(1)
        res = mcp.execute_tool("get_email_by_id", {"message_id": msg_id})
        return f"Email Details:<br><br>{_fmt(res)}"

    # ── Summarize ─────────────────────────────────
    m = re.search(r'\bsummariz[ei]\w*\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        email_obj = mcp.execute_tool("get_email_by_id", {"message_id": m.group(1)})
        res = mcp.execute_tool("summarize_email", {"email": str(email_obj.get("result", email_obj))})
        return f"📋 Summary:<br><br>{_fmt(res)}"

    # ── Reply ────────────────────────────────────
    m = re.search(r'\breply(?:\s+to)?\s+([a-fA-F0-9]{10,})\s+(?:saying|with)?\s*(.*)', low)
    if m:
        msg_id = m.group(1)
        body   = m.group(2).strip() or "Thank you!"
        res = mcp.execute_tool("reply_email", {"message_id": msg_id, "body": body})
        return f"✅ Reply sent!<br><br>{_fmt(res)}"

    # ── Forward ───────────────────────────────────
    m = re.search(r'\bforward\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\s+to\s+([\w.@+\-]+)', low)
    if m and _is_real_recipient(m.group(2)):
        res = mcp.execute_tool("forward_email", {"message_id": m.group(1), "to": m.group(2)})
        return f"↗️ Forwarded!<br><br>{_fmt(res)}"

    # ── Star / Unstar ─────────────────────────────
    m = re.search(r'\b(star|unstar)\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        tool = "star_email" if m.group(1) == "star" else "unstar_email"
        res = mcp.execute_tool(tool, {"message_id": m.group(2)})
        return f"⭐ Done!<br><br>{_fmt(res)}"

    # ── Archive ───────────────────────────────────
    m = re.search(r'\barchive\s+(?:email\s+|message\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        res = mcp.execute_tool("archive_email", {"message_id": m.group(1)})
        return f"📦 Archived!<br><br>{_fmt(res)}"

    # ── Mark read/unread ─────────────────────────
    m = re.search(r'\bmark\s+([a-fA-F0-9]{10,})\s+as\s+(read|unread)\b', low)
    if m:
        tool = "mark_as_read" if m.group(2) == "read" else "mark_as_unread"
        res = mcp.execute_tool(tool, {"message_id": m.group(1)})
        return f"✅ Marked as {m.group(2)}.<br><br>{_fmt(res)}"

    # ── Labels ────────────────────────────────────
    if re.search(r'\b(?:list|show|get|view)\s+(?:all\s+)?labels?\b', low):
        res = mcp.execute_tool("list_labels", {})
        return f"Your Gmail labels:<br><br>{_fmt(res)}"

    m = re.search(r'\bcreate\s+(?:a\s+)?label\s+(?:called|named)?\s*["\']?([^\s"\'?]+)["\']?', low)
    if m:
        name = m.group(1).lower()
        res = mcp.execute_tool("create_label", {"label_name": name})
        is_success = res.get("success", False)
        res_data = res.get("result", {})
        if is_success and isinstance(res_data, dict) and "note" in res_data:
            return f"⚠️ <b>{name}</b><br><br>{_fmt(res)}"
        elif is_success and not (isinstance(res_data, dict) and "error" in res_data):
            return f"🏷️ Label <b>{name}</b> created!<br><br>{_fmt(res)}"
        return f"Result for <b>{name}</b>:<br><br>{_fmt(res)}"

    m = re.search(r'\badd\s+label\s+["\']?([^\s"\']+)["\']?\s+to\s+([a-fA-F0-9]{10,})\b', low)
    if m:
        res = mcp.execute_tool("add_label", {"message_id": m.group(2), "label": m.group(1)})
        return f"✅ Label added!<br><br>{_fmt(res)}"

    # ── Search / Find emails by sender ──────────
    # Must have a real email address or explicit "from X" with a plausible name
    email_in_text = _extract_email(text)
    if email_in_text and re.search(r'\b(?:from|by|emails?\s+from|messages?\s+from)\b', low):
        res = mcp.execute_tool("search_emails", {"query": f"from:{email_in_text}", "limit": 10})
        ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
        state["last_viewed_ids"] = ids
        return f"Emails from <b>{email_in_text}</b>:<br><br>{_fmt(res)}"

    # "emails from NAME" — only if NAME looks like a real sender
    m = re.search(r'\b(?:emails?\s+from|messages?\s+from|from)\s+([^\s,?!.]+)', low)
    if m:
        sender = m.group(1)
        if _is_real_recipient(sender):
            res = mcp.execute_tool("search_emails", {"query": f"from:{sender}", "limit": 10})
            ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
            state["last_viewed_ids"] = ids
            return f"Emails from <b>{sender}</b>:<br><br>{_fmt(res)}"

    # "search emails about X" / "find emails with subject Y"
    m = re.search(
        r'\b(?:search|find|look\s+for)\s+(?:emails?\s+|messages?\s+)?(?:about|with\s+subject|regarding|with\s+keyword|containing)\s+(.+)',
        low
    )
    if m:
        q = m.group(1).strip().strip('"\'')
        if len(q) > 2:
            res = mcp.execute_tool("search_emails", {"query": q, "limit": 10})
            ids = [e['id'] for e in res.get('result', []) if isinstance(e, dict) and 'id' in e]
            state["last_viewed_ids"] = ids
            return f"Search results for <b>{q}</b>:<br><br>{_fmt(res)}"

    # ── Compose / Send email ─────────────────────
    # Pattern 1: "send/compose/write/draft email to X@Y.Z saying ..."
    compose_m = re.search(
        r'\b(?:send|create|write|compose|draft|make)\s+(?:an?\s+)?(?:email|mail|message)\s+to\s+([\w.@+\-]+)'
        r'(?:\s+(?:saying|with\s+(?:body|message|subject)|about|:))?\s*(.*)',
        low, re.DOTALL
    )
    if compose_m and not _is_real_recipient(compose_m.group(1)):
        compose_m = None

    # Pattern 2: "email to X@Y.Z saying ..."  (only with full email address or clear username)
    if not compose_m:
        m_tmp = re.search(
            r'\bemail\s+to\s+([\w.@+\-]+)(?:\s+(?:saying|with|about|:))?\s*(.*)',
            low, re.DOTALL
        )
        if m_tmp and _is_real_recipient(m_tmp.group(1)):
            compose_m = m_tmp

    if compose_m:
        to   = _extract_email(text) or compose_m.group(1)
        body = compose_m.group(2).strip() if compose_m.lastindex >= 2 else ""
        body = body or "Hello!"
        subject = body[:60].split(".")[0].strip() if body else "Message from G-Assistant"
        args = {"to": to, "subject": subject, "body": body}
        if state.get("last_attachment_path"):
            args["attachment_path"] = state.pop("last_attachment_path")

        # Detect action word
        action_word = re.search(r'\b(create|write|compose|draft|make|send)\b', low)
        action = action_word.group(1) if action_word else "send"
        if action in ("create", "write", "compose", "draft", "make"):
            res = mcp.execute_tool("draft_email", args)
            result = res.get("result", {}) if isinstance(res, dict) else {}
            did = result.get("id", "") if isinstance(result, dict) else ""
            if did:
                state["last_draft_id"] = did
                state["last_to"] = to
                state["last_body"] = body
                state["last_subject"] = subject
            return f"📝 Draft created to <b>{to}</b>!<br>Draft ID: <code>{did}</code><br>Say <b>'send it'</b> to send."
        else:
            res = mcp.execute_tool("send_email", args)
            return f"✅ Email sent to <b>{to}</b>!<br><br>{_fmt(res)}"

    # ── Draft / Create email (alternate phrasing) ─
    m = re.search(
        r'\b(?:create|make|write|compose)\s+(?:a\s+)?draft\s+(?:to|for)\s+([\w.@+-]+)\s+(?:saying|with body|:)?\s*(.*)',
        low
    )
    if m and _is_real_recipient(m.group(1)):
        to   = _extract_email(text) or m.group(1)
        body = m.group(2).strip() or "Hello!"
        subject = body[:50] if body else "Draft from G-Assistant"
        args = {"to": to, "subject": subject, "body": body}
        if state.get("last_attachment_path"):
            args["attachment_path"] = state.pop("last_attachment_path")
        res = mcp.execute_tool("draft_email", args)
        result = res.get("result", {}) if isinstance(res, dict) else {}
        draft_id = result.get("id", "")
        state["last_draft_id"] = draft_id
        state["last_to"]       = to
        state["last_body"]     = body
        state["last_subject"]  = subject
        return f"✅ Draft created to <b>{to}</b>!<br>Draft ID: <code>{draft_id}</code><br>Say <b>'send it'</b> to dispatch."

    # ── "send it" / "send that" / "send the draft" ─
    if re.search(r'^\s*(?:send|send\s+it|send\s+that|send\s+the\s+(?:draft|mail|email))\s*$', low) or \
       re.search(r'\b(?:send\s+it|send\s+that|send\s+the\s+(?:draft|mail|email))\b', low):
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

    # ── "delete it" / "discard draft" ─────────────
    if re.search(r'\b(?:delete\s+(?:the\s+)?last\s+draft|delete\s+it|discard\s+it|discard\s+(?:the\s+)?draft)\b', low):
        draft_id = state.get("last_draft_id")
        if draft_id:
            res = mcp.execute_tool("delete_draft", {"draft_id": draft_id})
            state.pop("last_draft_id", None)
            return "🗑️ Draft deleted."
        return "I don't have a record of your last draft to delete."

    # ── User pasted raw ID with "-> send" ─────────
    m = re.search(r'\b(r[\d]{10,})\s*[-=]>\s*(?:send|dispatch)', low)
    if m:
        draft_id = m.group(1)
        res = mcp.execute_tool("send_draft", {"draft_id": draft_id})
        return f"✅ Draft {draft_id} sent!<br><br>{_fmt(res)}"

    # ── List labels on an email ───────────────────
    m = re.search(r'\b(?:labels?|tags?)\s+(?:on|of|for)\s+(?:email\s+)?([a-fA-F0-9]{10,})\b', low)
    if m:
        email = mcp.execute_tool("get_email_by_id", {"message_id": m.group(1)})
        labels = email.get("result", {}).get("labels", []) if isinstance(email, dict) else []
        return f"Labels: <b>{', '.join(labels) if labels else 'None'}</b>"

    return None   # fall through to LLM


# ──────────────────────────────────────────────
# LLM tool-call extraction
# ──────────────────────────────────────────────
def _parse_tool_call(content):
    m = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL)
    snippet = m.group(1) if m else content

    m2 = re.search(r'\{\s*"tool"\s*:\s*"([^"]+)"\s*,\s*"args"\s*:\s*(\{.*?\})\s*\}', snippet, re.DOTALL)
    if m2:
        try:
            return {"tool": m2.group(1), "args": json.loads(m2.group(2))}
        except json.JSONDecodeError:
            pass

    try:
        data = json.loads(snippet.strip())
        if isinstance(data, dict) and "tool" in data:
            return data
    except:
        pass

    return None


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────
SYSTEM_PROMPT_TEMPLATE = """You are G-Assistant, a smart and helpful AI Gmail assistant.

Context:
- Current date: {current_date}
- Recently listed email IDs: {viewed_ids}
- Last draft ID: {last_draft_id}

Instructions:
1. For Gmail actions, output ONLY a JSON tool call — no explanation before it.
2. If the user says "these", "them", "the last one" and viewed_ids is set, use those IDs.
3. If viewed_ids is None and you need an ID, call get_emails(limit=1) first.
4. For general questions (not Gmail actions), reply normally in plain English.
5. NEVER call send_email unless the user provided a clear recipient email address.

Tool call format (use ONLY this):
```json
{{"tool": "tool_name", "args": {{"param": "value"}}}}
```

Available Tools:
{tool_list}
"""


def run_agent(user_input, mcp, state):
    # ── Fast Python intent router ───────────────
    intent_reply = _intent_detect(user_input, mcp, state)
    if intent_reply is not None:
        return intent_reply

    # ── LLM fallback ────────────────────────────
    from app.core.llm_client import call_model
    from app.integrations.gmail.registry import GMAIL_TOOLS
    import datetime

    tl = "\n".join([f"- {k}" for k in GMAIL_TOOLS.keys()])
    v_ids = state.get("last_viewed_ids", [])
    d_id  = state.get("last_draft_id", "None")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sys_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        current_date=now_str,
        viewed_ids=", ".join(v_ids) if v_ids else "None",
        last_draft_id=d_id,
        tool_list=tl
    )

    if "history" not in state:
        state["history"] = [{"role": "system", "content": sys_prompt}]
    else:
        state["history"][0] = {"role": "system", "content": sys_prompt}

    state["history"].append({"role": "user", "content": user_input})

    if len(state["history"]) > 31:
        state["history"] = [state["history"][0]] + state["history"][-30:]

    reply_text = ""
    for _ in range(MAX_TOOL_LOOPS):
        try:
            resp    = call_model(state["history"])
            msg     = resp.get("choices", [{}])[0].get("message", {})
            content = msg.get("content", "")
            state["history"].append(msg)

            tool_call = _parse_tool_call(content)
            if tool_call:
                tool_name = tool_call["tool"]
                args      = tool_call.get("args", {})
                if state.get("last_attachment_path") and tool_name in ["send_email", "draft_email"]:
                    args["attachment_path"] = state.pop("last_attachment_path")
                res = mcp.execute_tool(tool_name, args)
                if tool_name == "draft_email" and isinstance(res, dict):
                    did = res.get("result", {}).get("id") if isinstance(res.get("result"), dict) else None
                    if did:
                        state["last_draft_id"] = did
                state["history"].append({
                    "role": "user",
                    "content": f"Tool result: {json.dumps(res)}"
                })
                continue

            reply_text = content
            break

        except Exception as e:
            reply_text = f"<b style='color:#ef4444'>Error:</b> {e}"
            break

    if not reply_text:
        last = state["history"][-1].get("content", "") if state["history"] else ""
        if "Tool result:" in last:
            try:
                raw = last.split("Tool result: ", 1)[1]
                reply_text = _fmt(json.loads(raw))
            except:
                reply_text = last
        else:
            reply_text = "I wasn't able to complete that. Please try rephrasing your request."

    return reply_text
