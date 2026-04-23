"""
Gmail AI / Analysis Tools

All single-email tools accept message_id (per orchestrator contract),
fetch the email body via core.get_email_by_id, then call the LLM.
"""

from app.core.llm_client import call_model
from app.integrations.gmail import core

def _invoke_ai(prompt):
    messages = [{"role": "user", "content": prompt}]
    result = call_model(messages)
    return result.get('choices', [{}])[0].get('message', {}).get('content', '')

def _fetch_email_body(message_id):
    """Fetch email and return a readable string for the LLM."""
    try:
        email = core.get_email_by_id(message_id)
        if isinstance(email, dict):
            parts = [f"From: {email.get('sender', 'unknown')}",
                     f"Subject: {email.get('subject', 'no subject')}",
                     f"Date: {email.get('date', 'unknown')}",
                     f"Body: {email.get('body', email.get('snippet', ''))}"]
            return "\n".join(parts)
        return str(email)
    except Exception as e:
        return f"[Could not fetch email {message_id}: {e}]"

def _fetch_recent_emails(limit=10):
    """Fetch recent emails and return a readable string for the LLM."""
    try:
        data = core.get_emails(limit=limit)
        emails = data.get("emails", []) if isinstance(data, dict) else []
        lines = []
        for e in emails:
            lines.append(f"- From: {e.get('sender','?')} | Subject: {e.get('subject','?')} | Snippet: {e.get('snippet','')}")
        return "\n".join(lines)
    except Exception as e:
        return f"[Could not fetch recent emails: {e}]"

# ── Single-email AI tools ───────────────────────────────────────────────────

def summarize_email(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Summarize this email in 3 bullets: intent, deadlines, action.\n\n{email_text}")

def classify_email(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Classify this email into ONE of: work / personal / finance / promotion / support / meeting / newsletter / spam / other.\n\n{email_text}")

def detect_urgency(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Rate the urgency of this email: low / medium / high / critical. Give a one-sentence reason.\n\n{email_text}")

def detect_action_required(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Does this email require action? Answer yes or no and briefly state what action is needed.\n\n{email_text}")

def sentiment_analysis(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Analyse the tone/sentiment of this email: positive / neutral / negative / mixed. Briefly explain why.\n\n{email_text}")

def extract_tasks(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Extract all actionable tasks from this email as a numbered checklist.\n\n{email_text}")

def extract_dates(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Extract every date, deadline, or time reference in this email with context.\n\n{email_text}")

def extract_contacts(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Extract names, email addresses, phone numbers, and organisations mentioned in this email.\n\n{email_text}")

def extract_links(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"List every URL or link mentioned in this email.\n\n{email_text}")

def draft_reply(message_id, tone="professional"):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Draft a {tone} reply to the following email. Keep it concise and on-topic.\n\n{email_text}")

def generate_followup(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Draft a short, polite follow-up to this email.\n\n{email_text}")

def auto_reply(message_id):
    email_text = _fetch_email_body(message_id)
    return _invoke_ai(f"Write a 2-3 sentence auto-acknowledgement reply to this email.\n\n{email_text}")

def rewrite_email(message_id, tone="professional"):
    email_text = _fetch_email_body(message_id)
    prompt = (f"Rewrite the following email in a {tone} tone. "
              f"Maintain the same meaning and intent but adjust the style:\n\n{email_text}")
    return _invoke_ai(prompt)

def translate_email(message_id, language="English"):
    email_text = _fetch_email_body(message_id)
    prompt = (f"Translate the following email to {language}. "
              f"Preserve the meaning, tone, and formatting as much as possible:\n\n{email_text}")
    return _invoke_ai(prompt)

# ── Multi-email AI tools ──────────────────────────────────────────────────

def summarize_emails(limit=10):
    emails_text = _fetch_recent_emails(limit)
    return _invoke_ai(f"Group these emails by topic and flag any urgent items.\n\n{emails_text}")

def auto_label_emails(limit=20):
    emails_text = _fetch_recent_emails(limit)
    return _invoke_ai(f"Suggest Gmail labels for these emails (e.g. Work, Personal, Finance, Promotions).\n\n{emails_text}")

def auto_archive_promotions():
    return _invoke_ai("Suggest a list of promotional email sender patterns that could be auto-archived.")

def auto_reply_rules():
    return _invoke_ai("Suggest 3 smart auto-reply rules based on common email patterns.")

# ── Back-compat aliases (kept in core.py, not duplicated here) ─────────────
