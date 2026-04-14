from app.core.llm_client import call_model

def _invoke_ai(prompt):
    messages = [{"role": "user", "content": prompt}]
    result = call_model(messages)
    return result.get('choices', [{}])[0].get('message', {}).get('content', '')

def summarize_email(email):
    return _invoke_ai(f"Summarize this email in 3 bullets: intent, deadlines, action.\n\nEmail: {email}")

def summarize_emails(emails):
    return _invoke_ai(f"Group these emails by topic, flag urgencies.\n\nEmails: {emails}")

def classify_email(email):
    return _invoke_ai(f"Classify this email (work/personal/finance/promotion/support/meeting/newsletter/spam/other): {email}")

def detect_urgency(email): return "low / medium / high / critical + reason"
def detect_action_required(email): return "yes/no + what action"
def sentiment_analysis(email): return "positive / neutral / negative / mixed + explanation"
def extract_tasks(email): return "Numbered checklist of to-dos"
def extract_dates(email): return "All dates/deadlines with context"
def extract_contacts(email): return "Names, emails, phones, orgs"
def extract_links(email): return ["https://example.com"]
def draft_reply(email): return "Professional reply draft"
def generate_followup(email): return "Short polite follow-up"
def auto_reply(email): return "2-3 sentence auto-acknowledgement"
def rewrite_email(email, tone="professional"): return "Rewrite in given tone"
def translate_email(email, language): return "Translation"
def auto_label_emails(): return "Classify all unread -> suggest labels"
def auto_archive_promotions(): return "Archive promos"
def auto_reply_rules(): return "Suggest auto-replies"
def count_emails_by_sender(): return {}
def email_activity_summary(): return "activity summary"
def most_frequent_contacts(): return []
