import threading
import time
import base64
import json
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from app.core.config import TOKEN_FILE, GMAIL_SCOPES as SCOPES, SCHEDULE_STORE as schedule_store

_thread_local = threading.local()

def authenticate_gmail():
    if hasattr(_thread_local, "gmail_service") and _thread_local.gmail_service is not None:
        return _thread_local.gmail_service

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        except Exception:
            TOKEN_FILE.unlink(missing_ok=True)
            creds = None
    if not creds or not creds.valid:
        raise PermissionError(
            "Gmail not authenticated. Run `python auth.py` then restart the app."
        )
    
    # Create thread-safe instance
    service = build("gmail", "v1", credentials=creds)
    _thread_local.gmail_service = service
    return service

def reset_gmail_service():
    if hasattr(_thread_local, "gmail_service"):
        _thread_local.gmail_service = None

def gmail_call(api_callable, retries=3, backoff=1.5):
    last_error = None
    for attempt in range(retries):
        try:
            return api_callable()
        except HttpError as exc:
            status = exc.resp.status if hasattr(exc, "resp") else 0
            if status == 401:
                reset_gmail_service()
                last_error = exc
                continue
            if status in (429, 500, 502, 503, 504):
                last_error = exc
                if attempt < retries - 1: time.sleep(backoff * (attempt + 1))
                continue
            raise RuntimeError(f"Gmail API error ({status}): {exc}") from exc
        except (OSError, ConnectionError) as exc:
            last_error = exc
            if attempt < retries - 1: time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Gmail connection failed after {retries} attempts: {last_error}")

# Core utilities
def validate_email_address(email):
    import re
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))

def extract_primary_email_address(raw):
    # parse Name <email> format
    import re
    match = re.search(r'<([^>]+)>', raw)
    return match.group(1) if match else raw.strip()

def sanitize_email_content(text):
    return text.replace("<script>", "").replace("</script>", "")

def now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

def load_json_file(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json_file(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def log_email_action(action):
    from app.core.config import AUDIT_LOG_FILE
    with open(AUDIT_LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps({"time": now_iso(), "action": action}) + "\n")

def fetch_message(service, msg_id):
    return gmail_call(lambda: service.users().messages().get(userId='me', id=msg_id).execute())

def parse_email_body(payload):
    body = "No content."
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                data = part['body']['data']
                body = base64.urlsafe_b64decode(data).decode('utf-8')
                break
    elif 'body' in payload and 'data' in payload['body']:
        body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
    return body[:6000]

def normalize_email(raw_message):
    payload = raw_message.get('payload', {})
    headers = payload.get('headers', [])
    header_dict = {h['name'].lower(): h['value'] for h in headers}
    
    return {
        "id": raw_message.get("id"),
        "thread_id": raw_message.get("threadId"),
        "from": header_dict.get("from", ""),
        "to": header_dict.get("to", ""),
        "cc": header_dict.get("cc", ""),
        "bcc": header_dict.get("bcc", ""),
        "reply_to": header_dict.get("reply-to", ""),
        "subject": header_dict.get("subject", ""),
        "date": header_dict.get("date", ""),
        "snippet": raw_message.get("snippet", ""),
        "body": parse_email_body(payload),
        "labels": raw_message.get("labelIds", []),
        "attachments": []
    }

# Read APIs
def get_emails(limit=20):
    """
    Get recent emails from inbox.
    
    Returns:
        dict: {
            "success": True/False,
            "emails": [...],
            "count": N,
            "error": "..." (if failed)
        }
    """
    try:
        service = authenticate_gmail()
    except PermissionError as e:
        return {
            "success": False,
            "emails": [],
            "count": 0,
            "error": f"Authentication error: {str(e)}"
        }
    
    try:
        results = gmail_call(lambda: service.users().messages().list(userId='me', maxResults=limit).execute())
        messages = results.get('messages', [])
        emails = []
        for msg in messages:
            try:
                raw = fetch_message(service, msg['id'])
                emails.append(normalize_email(raw))
            except:
                continue
        return {
            "success": True,
            "emails": emails,
            "count": len(emails)
        }
    except Exception as e:
        return {
            "success": False,
            "emails": [],
            "count": 0,
            "error": f"Failed to get emails: {str(e)}"
        }

def get_email_by_id(message_id):
    service = authenticate_gmail()
    raw = fetch_message(service, message_id)
    return normalize_email(raw)

def get_unread_emails(limit=10):
    return search_emails("is:unread", limit=limit)
    
def get_starred_emails(limit=10):
    return search_emails("is:starred", limit=limit)

def search_emails(query, limit=10):
    """
    Search emails using Gmail query syntax.
    
    Args:
        query: Gmail search query (e.g., "from:glassdoor", "is:unread", "subject:invoice")
        limit: Maximum number of results (default 10)
    
    Returns:
        dict: {
            "success": True/False,
            "emails": [...],
            "count": N,
            "query": query,
            "error": "..." (if failed)
        }
    """
    try:
        service = authenticate_gmail()
    except PermissionError as e:
        return {
            "success": False,
            "emails": [],
            "count": 0,
            "query": query,
            "error": f"Authentication error: {str(e)}"
        }
    
    # Check if query is just a message ID
    if len(query) == 16 and re.match(r'^[a-fA-F0-9]+$', query):
        try:
            email = get_email_by_id(query)
            return {
                "success": True,
                "emails": [email],
                "count": 1,
                "query": query
            }
        except Exception as e:
            return {
                "success": False,
                "emails": [],
                "count": 0,
                "query": query,
                "error": f"Message ID lookup failed: {str(e)}"
            }
    
    try:
        results = gmail_call(lambda: service.users().messages().list(
            userId='me', 
            maxResults=limit, 
            q=query
        ).execute())
        
        messages = results.get('messages', [])
        
        if not messages:
            return {
                "success": True,
                "emails": [],
                "count": 0,
                "query": query,
                "message": f"No emails found matching query: '{query}'"
            }
        
        emails = []
        errors = 0
        for msg in messages:
            try:
                raw = fetch_message(service, msg['id'])
                emails.append(normalize_email(raw))
            except Exception as e:
                errors += 1
                continue
        
        return {
            "success": True,
            "emails": emails,
            "count": len(emails),
            "query": query,
            "errors_during_fetch": errors if errors > 0 else None
        }
        
    except HttpError as e:
        status = e.resp.status if hasattr(e, 'resp') else 0
        return {
            "success": False,
            "emails": [],
            "count": 0,
            "query": query,
            "error": f"Gmail API error ({status}): {str(e)}"
        }
    except Exception as e:
        return {
            "success": False,
            "emails": [],
            "count": 0,
            "query": query,
            "error": f"Search failed: {str(e)}"
        }

def get_emails_by_sender(sender): 
    return search_emails(f"from:{sender}", limit=10)

def get_emails_by_label(label): 
    return search_emails(f"label:{label}", limit=10)

def get_emails_by_date_range(start, end):
    # Gmail after:/before: are exclusive at midnight UTC.
    # If start == end, user wants that single day; add 1 day to end so range is inclusive.
    if start == end:
        from datetime import datetime, timedelta
        dt = datetime.strptime(start, "%Y/%m/%d")
        end = (dt + timedelta(days=1)).strftime("%Y/%m/%d")
    return search_emails(f"after:{start} before:{end}", limit=10)

def get_email_thread(thread_id):
    service = authenticate_gmail()
    results = gmail_call(lambda: service.users().threads().get(userId='me', id=thread_id).execute())
    messages = results.get('messages', [])
    emails = [normalize_email(m) for m in messages]
    return emails

def _build_multipart_message(to, subject, body, attachment_path=None):
    from pathlib import Path
    import mimetypes
    message = MIMEMultipart()
    message['to'] = to
    message['subject'] = subject
    message.attach(MIMEText(body))
    
    if attachment_path and Path(attachment_path).exists():
        path = Path(attachment_path)
        content_type, encoding = mimetypes.guess_type(path.name)
        if content_type is None or encoding is not None:
            content_type = 'application/octet-stream'
        main_type, sub_type = content_type.split('/', 1)
        
        with open(path, 'rb') as f:
            part = MIMEBase(main_type, sub_type)
            part.set_payload(f.read())
            
        from email import encoders
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename="{path.name}"')
        message.attach(part)
        
    return base64.urlsafe_b64encode(message.as_bytes()).decode()

def send_email(to, subject="Contact from G-Assistant", body="(No content)", attachment_path=None):
    if not to:
        return {"success": False, "error": "Recipient 'to' address is required."}
    service = authenticate_gmail()
    encoded = _build_multipart_message(to, subject, body, attachment_path)
    body_dict = {'raw': encoded}
    try:
        res = gmail_call(lambda: service.users().messages().send(userId='me', body=body_dict).execute())
        log_email_action({"type": "send", "to": to, "subject": subject, "has_attachment": bool(attachment_path)})
        return res
    except Exception as e:
        return {"success": False, "error": str(e)}

def draft_email(to, subject, body, attachment_path=None):
    service = authenticate_gmail()
    encoded = _build_multipart_message(to, subject, body, attachment_path)
    body_dict = {'message': {'raw': encoded}}
    res = gmail_call(lambda: service.users().drafts().create(userId='me', body=body_dict).execute())
    log_email_action({"type": "draft", "to": to, "subject": subject, "has_attachment": bool(attachment_path)})
    return res

def update_draft(draft_id, body): pass
def send_draft(draft_id):
    service = authenticate_gmail()
    clean_id = str(draft_id).strip()
    # sometimes LLMs pass the message ID instead of draft ID
    body = {'id': clean_id}
    try:
        res = gmail_call(lambda: service.users().drafts().send(userId='me', body=body).execute())
        log_email_action({"type": "send_draft", "draft_id": clean_id})
        return res
    except Exception as e:
        return {"error": str(e), "message": f"Failed to send draft {clean_id}. Make sure this is the draft_id, not the message_id."}

def update_draft(draft_id, body):
    """Update a draft by draft_id or message_id.
    
    If a message_id is passed, it will first try to find the corresponding draft.
    """
    service = authenticate_gmail()
    clean_id = str(draft_id).strip()
    
    # Try to find the actual draft ID if message ID was passed
    actual_draft_id = clean_id
    try:
        drafts = gmail_call(lambda: service.users().drafts().list(userId='me').execute())
        if drafts and 'drafts' in drafts:
            for draft in drafts['drafts']:
                if draft.get('message', {}).get('id') == clean_id:
                    actual_draft_id = draft['id']
                    break
    except:
        pass  # Continue with original ID if list fails
    
    message = MIMEMultipart()
    msg = MIMEText(body)
    message.attach(msg)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body_dict = {'message': {'raw': encoded}}
    
    try:
        return gmail_call(lambda: service.users().drafts().update(userId='me', id=actual_draft_id, body=body_dict).execute())
    except Exception as e:
        return {"error": str(e), "message": f"Failed to update draft {clean_id}. Ensure this is a valid draft ID or message ID that has a draft."}

def delete_draft(draft_id):
    """Delete a draft by draft_id or message_id.
    
    If a message_id is passed, it will first try to find the corresponding draft.
    """
    service = authenticate_gmail()
    clean_id = str(draft_id).strip()
    
    try:
        # First, try to delete as draft_id directly
        return gmail_call(lambda: service.users().drafts().delete(userId='me', id=clean_id).execute())
    except Exception as e:
        error_str = str(e)
        # If draft not found, try to find draft by message ID
        if 'not found' in error_str.lower() or '404' in error_str:
            try:
                drafts = gmail_call(lambda: service.users().drafts().list(userId='me').execute())
                if drafts and 'drafts' in drafts:
                    for draft in drafts['drafts']:
                        if draft.get('message', {}).get('id') == clean_id:
                            # Found the draft for this message
                            return gmail_call(lambda: service.users().drafts().delete(userId='me', id=draft['id']).execute())
                return {"error": f"Draft not found for ID: {clean_id}", "message": "No draft exists with this ID. The message ID may not have a corresponding draft."}
            except Exception as inner_e:
                return {"error": str(inner_e), "message": f"Failed to find and delete draft for ID: {clean_id}"}
        return {"error": error_str, "message": f"Failed to delete draft {clean_id}"}

def reply_email(message_id, body):
    service = authenticate_gmail()
    original = fetch_message(service, message_id)
    thread_id = original.get('threadId')
    headers = original.get('payload', {}).get('headers', [])
    header_dict = {h['name'].lower(): h['value'] for h in headers}
    
    message = MIMEMultipart()
    message['to'] = header_dict.get('reply-to', header_dict.get('from', ''))
    message['subject'] = header_dict.get('subject', 'Re: ')
    message['In-Reply-To'] = header_dict.get('message-id', '')
    message['References'] = header_dict.get('message-id', '')
    msg = MIMEText(body)
    message.attach(msg)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body_dict = {'raw': encoded, 'threadId': thread_id}
    res = gmail_call(lambda: service.users().messages().send(userId='me', body=body_dict).execute())
    return res

def reply_all(message_id, body):
    return reply_email(message_id, body)

def forward_email(message_id, to):
    service = authenticate_gmail()
    original = fetch_message(service, message_id)
    thread_id = original.get('threadId')
    message = MIMEMultipart()
    message['to'] = to
    message['subject'] = 'Fwd: ' + next((h['value'] for h in original['payload']['headers'] if h['name'].lower() == 'subject'), '')
    msg = MIMEText("Forwarded message:\n\n" + parse_email_body(original.get('payload', {})))
    message.attach(msg)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body_dict = {'raw': encoded}
    return gmail_call(lambda: service.users().messages().send(userId='me', body=body_dict).execute())

def list_labels():
    service = authenticate_gmail()
    res = gmail_call(lambda: service.users().labels().list(userId='me').execute())
    return res.get('labels', [])

def delete_label(label_name):
    """Delete a Gmail label by name. Looks up the label ID first, then deletes it."""
    service = authenticate_gmail()
    labels  = list_labels()
    # Exact match first, then case-insensitive substring fallback
    target = next(
        (l for l in labels if l.get("name", "").lower() == label_name.lower()),
        None,
    ) or next(
        (l for l in labels if label_name.lower() in l.get("name", "").lower()),
        None,
    )
    if not target:
        raise ValueError(f"Label '{label_name}' not found. Use 'list labels' to see available labels.")
    if target.get("type") == "system":
        raise ValueError(f"'{target['name']}' is a system label and cannot be deleted.")
    gmail_call(lambda: service.users().labels().delete(userId='me', id=target["id"]).execute())
    return f"Label '{target['name']}' deleted successfully."


def _modify_labels(message_id, add_labels, remove_labels):
    service = authenticate_gmail()
    body = {'addLabelIds': add_labels, 'removeLabelIds': remove_labels}
    return gmail_call(lambda: service.users().messages().modify(userId='me', id=message_id, body=body).execute())

def _get_label_id(service, label_name):
    """Resolve label name to ID. Returns the ID or None if not found."""
    try:
        labels = gmail_call(lambda: service.users().labels().list(userId='me').execute())
        for lbl in labels.get('labels', []):
            if lbl.get('name', '').lower() == label_name.lower():
                return lbl['id']
        return None
    except:
        return None

def add_label(message_id, label):
    service = authenticate_gmail()
    label_id = _get_label_id(service, label)
    if not label_id:
        return {"error": f"Label '{label}' not found. Use 'list labels' to see available labels."}
    return _modify_labels(message_id, [label_id], [])

def remove_label(message_id, label):
    service = authenticate_gmail()
    label_id = _get_label_id(service, label)
    if not label_id:
        return {"error": f"Label '{label}' not found. Use 'list labels' to see available labels."}
    return _modify_labels(message_id, [], [label_id])
def create_label(label_name):
    service = authenticate_gmail()
    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show"
    }
    try:
        res = gmail_call(lambda: service.users().labels().create(userId='me', body=body).execute())
        return res
    except Exception as e:
        err_str = str(e)
        if "409" in err_str:
            return {"name": label_name, "note": "Label already exists."}
        if "400" in err_str:
            # Fallback for simple creation without visibility flags if Gmail rejects it
            try:
                res = gmail_call(lambda: service.users().labels().create(userId='me', body={"name": label_name}).execute())
                return res
            except Exception as inner_e:
                return {"error": f"Failed (400) and fallback failed: {inner_e}"}
        return {"error": err_str}

def mark_as_read(message_id): return _modify_labels(message_id, [], ['UNREAD'])
def mark_as_unread(message_id): return _modify_labels(message_id, ['UNREAD'], [])
def star_email(message_id): return _modify_labels(message_id, ['STARRED'], [])
def unstar_email(message_id): return _modify_labels(message_id, [], ['STARRED'])

def archive_email(message_id): return _modify_labels(message_id, [], ['INBOX'])
def unarchive_email(message_id): return _modify_labels(message_id, ['INBOX'], [])
def move_to_folder(message_id, folder):
    # folder in Gmail is usually a label name. 
    # We remove INBOX and add the target label.
    return _modify_labels(message_id, [folder], ['INBOX'])

def _validate_message_id(mid: str) -> str:
    """
    Gmail message IDs are hex strings (e.g. 18e7a3c9f1b2d4e5).
    Reject anything that is clearly not a Gmail ID so we get a clear error
    instead of a confusing 400 from the API.
    """
    mid = str(mid).strip()
    import re as _re
    # Must be 10-32 chars, hex only
    if not _re.fullmatch(r'[a-fA-F0-9]{10,32}', mid):
        raise ValueError(
            f"'{mid}' does not look like a valid Gmail message ID. "
            "Gmail IDs are hex strings (e.g. 18e7a3c9f1b2d4e5). "
            "Use 'show my emails' to see real IDs."
        )
    return mid


def trash_email(message_id=None, id=None):
    """Accept 'message_id' or 'id' to be robust against LLM arg naming."""
    target = message_id or id
    if not target:
        return {"success": False, "error": "message_id is required"}
    try:
        service = authenticate_gmail()
        ids = target if isinstance(target, (list, tuple)) else [target]
        results = []
        for mid in ids:
            mid_str = _validate_message_id(mid)
            res = gmail_call(lambda m=mid_str: service.users().messages().trash(userId='me', id=m).execute())
            results.append(res)
        return {"success": True, "result": results if len(results) > 1 else results[0]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def restore_email(message_id=None, id=None):
    target = message_id or id
    if not target:
        return {"success": False, "error": "message_id is required"}
    try:
        service = authenticate_gmail()
        ids = target if isinstance(target, (list, tuple)) else [target]
        results = []
        for mid in ids:
            mid_str = _validate_message_id(mid)
            res = gmail_call(lambda m=mid_str: service.users().messages().untrash(userId='me', id=m).execute())
            results.append(res)
        return {"success": True, "result": results if len(results) > 1 else results[0]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def delete_email(message_id=None, id=None):
    """Permanently delete email - NOT the same as trash! Use trash_email to move to trash."""
    target = message_id or id
    if not target:
        return {"success": False, "error": "message_id is required"}
    try:
        service = authenticate_gmail()
        ids = target if isinstance(target, (list, tuple)) else [target]
        results = []
        for mid in ids:
            mid_str = _validate_message_id(mid)
            res = gmail_call(lambda m=mid_str: service.users().messages().delete(userId='me', id=m).execute())
            results.append(res)
        return {"success": True, "result": results if len(results) > 1 else results[0]}
    except Exception as e:
        return {"success": False, "error": str(e)}

def get_attachments(message_id):
    service = authenticate_gmail()
    msg = fetch_message(service, message_id)
    payload = msg.get('payload', {})
    parts = payload.get('parts', [])
    attachments = []
    
    def _find_attachments(pts):
        for p in pts:
            if p.get('filename') and p.get('body', {}).get('attachmentId'):
                attachments.append({
                    "id": p['body']['attachmentId'],
                    "filename": p['filename'],
                    "mime_type": p['mimeType'],
                    "size": p['body'].get('size', 0)
                })
            if 'parts' in p:
                _find_attachments(p['parts'])
                
    _find_attachments(parts)
    return attachments

def save_attachment_to_disk(message_id, attachment_id, filename):
    from app.core.config import DATA_DIR
    service = authenticate_gmail()
    
    # First verify the message exists and check for attachments
    try:
        msg = fetch_message(service, message_id)
        attachments = get_attachments(message_id)
        
        if not attachments:
            return {"error": f"No attachments found on message {message_id}", "attachments": []}
        
        # Verify the attachment_id exists on this message
        valid_ids = [a['id'] for a in attachments]
        if attachment_id not in valid_ids:
            return {
                "error": f"Attachment ID '{attachment_id}' not found on message {message_id}",
                "available_attachments": attachments
            }
    except Exception as e:
        return {"error": f"Failed to verify message/attachments: {str(e)}"}
    
    try:
        res = gmail_call(lambda: service.users().messages().attachments().get(
            userId='me', messageId=message_id, id=attachment_id).execute())
        
        data = base64.urlsafe_b64decode(res['data'])
        path = DATA_DIR / "downloads"
        path.mkdir(exist_ok=True)
        target = path / filename
        target.write_bytes(data)
        return {"success": True, "path": str(target), "size": len(data)}
    except Exception as e:
        return {"error": f"Failed to download attachment: {str(e)}"}

download_attachment = save_attachment_to_disk

def count_emails_by_sender():
    service = authenticate_gmail()
    results = gmail_call(lambda: service.users().messages().list(userId='me', maxResults=100).execute())
    messages = results.get('messages', [])
    counts = {}
    for m in messages:
        msg = fetch_message(service, m['id'])
        headers = msg.get('payload', {}).get('headers', [])
        sender = next((h['value'] for h in headers if h['name'].lower() == 'from'), 'Unknown')
        counts[sender] = counts.get(sender, 0) + 1
    return counts

def most_frequent_contacts():
    counts = count_emails_by_sender()
    sorted_contacts = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return [c[0] for c in sorted_contacts[:10]]

def email_activity_summary():
    counts = count_emails_by_sender()
    total = sum(counts.values())
    top = most_frequent_contacts()
    return f"Analyzed last 100 emails. Total senders: {len(counts)}. Top contact: {top[0] if top else 'N/A'}"

def validate_email_address(email):
    import re
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))

def sanitize_email_content(content):
    return content.strip().replace("\r\n", "\n")

def log_email_action(action_dict):
    from app.core.config import DATA_DIR
    import time
    log_file = DATA_DIR / "email_actions.log"
    with open(log_file, "a") as f:
        log_entry = json.dumps({"timestamp": time.time(), **action_dict})
        f.write(log_entry + "\n")

def audit_email_history():
    from app.core.config import DATA_DIR
    log_file = DATA_DIR / "email_actions.log"
    if not log_file.exists(): return []
    with open(log_file, "r") as f:
        return [json.loads(line) for line in f.readlines()]

# Remaining Placeholders
def schedule_email(to, subject, body, send_at): return {"error": "Scheduling requires local cron/task setup. Coming soon."}
def confirm_action(action, target): return True
