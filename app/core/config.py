import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CREDENTIALS_DIR = PROJECT_ROOT / "credentials"

def _first_existing(*paths):
    for p in paths:
        if p.exists(): return p
    return paths[0]

CREDENTIALS_FILE = _first_existing(CREDENTIALS_DIR / "credentials.json", PROJECT_ROOT / "credentials.json")
TOKEN_FILE = _first_existing(CREDENTIALS_DIR / "token.json", PROJECT_ROOT / "token.json")

DATA_DIR = PROJECT_ROOT / "data"
DOWNLOADS_DIR = DATA_DIR / "downloads"
UPLOADS_DIR = DATA_DIR / "uploads"
DRAFTS_STORE = DATA_DIR / "draft_cache.json"
SCHEDULE_STORE = DATA_DIR / "scheduled_emails.json"
REMINDER_STORE = DATA_DIR / "email_reminders.json"
AUDIT_LOG_FILE = DATA_DIR / "email_audit_log.jsonl"
ATTACHMENT_CACHE_FILE = DATA_DIR / "attachment_cache.json"

for d in [DATA_DIR, DOWNLOADS_DIR, UPLOADS_DIR, CREDENTIALS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1/chat/completions")
MODEL_NAME = os.getenv("MODEL_NAME", "gemma-4-e2b-it")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.6"))
AGENT_NAME = os.getenv("AGENT_NAME", "G-Assistant")
CONTEXT_WINDOW = int(os.getenv("CONTEXT_WINDOW", "30"))
MAX_TOOL_LOOPS = int(os.getenv("MAX_TOOL_LOOPS", "3"))
MAX_HISTORY_MSG_CHARS = int(os.getenv("MAX_HISTORY_MSG_CHARS", "1500"))
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "8000"))
GMAIL_SCOPES  = ["https://www.googleapis.com/auth/gmail.modify"]
DOCS_SCOPES   = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ALL_SCOPES    = GMAIL_SCOPES + DOCS_SCOPES + SHEETS_SCOPES
