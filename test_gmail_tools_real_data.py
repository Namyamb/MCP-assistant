#!/usr/bin/env python3
"""
Gmail Tools Test Suite with REAL DATA
=====================================

Tests ALL Gmail tools using actual data from your Gmail account.

Prerequisites:
    1. Run 'python auth.py' to authenticate with Gmail first
    2. Have some emails in your Gmail account

Usage:
    python test_gmail_tools_real_data.py [--tool TOOL_NAME] [--category CATEGORY] [--random N] [--verbose]

Examples:
    python test_gmail_tools_real_data.py                      # Test all tools
    python test_gmail_tools_real_data.py --tool send_email    # Test only send_email
    python test_gmail_tools_real_data.py --category core      # Test Core Email category
    python test_gmail_tools_real_data.py --random 3           # 3 random queries per tool

Output:
    - Console: Colored progress and summary
    - JSON: test_results_TIMESTAMP.json
    - Report: test_report_TIMESTAMP.txt
"""

import sys
import json
import time
import random
import argparse
import traceback
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
from enum import Enum

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import build_server
from app.core.orchestrator import run_agent
from app.integrations.gmail.registry import GMAIL_TOOLS
from app.integrations.gmail.core import (
    authenticate_gmail,
    get_emails,
    get_unread_emails,
    get_starred_emails,
    search_emails,
    gmail_call,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Colors for Console Output
# ═══════════════════════════════════════════════════════════════════════════════

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    SUCCESS = "\033[92m"
    WARNING = "\033[93m"
    ERROR = "\033[91m"
    INFO = "\033[94m"
    CORE = "\033[96m"
    SEND = "\033[95m"
    LABELS = "\033[94m"
    ATTACHMENTS = "\033[93m"
    AI = "\033[92m"


class ToolCategory(Enum):
    CORE = "Core Email Operations"
    SEND = "Sending & Drafting"
    LABELS = "Labels & Organization"
    ATTACHMENTS = "Attachments"
    SCHEDULING = "Scheduling & Reminders"
    SECURITY = "Security & Validation"
    ANALYTICS = "Analytics"
    AI = "AI-Powered Tools"


CATEGORY_COLORS = {
    ToolCategory.CORE: Colors.CORE,
    ToolCategory.SEND: Colors.SEND,
    ToolCategory.LABELS: Colors.LABELS,
    ToolCategory.ATTACHMENTS: Colors.ATTACHMENTS,
    ToolCategory.SCHEDULING: Colors.INFO,
    ToolCategory.SECURITY: Colors.WARNING,
    ToolCategory.ANALYTICS: Colors.INFO,
    ToolCategory.AI: Colors.AI,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Real Data Fetcher
# ═══════════════════════════════════════════════════════════════════════════════

class GmailDataFetcher:
    """Fetches real data from Gmail account for use in test queries."""
    
    def __init__(self, max_emails: int = 20):
        self.max_emails = max_emails
        self._data: Optional[Dict[str, Any]] = None
    
    def _ensure_auth(self) -> bool:
        try:
            authenticate_gmail()
            return True
        except PermissionError:
            return False
    
    def fetch(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        
        if not self._ensure_auth():
            print(f"{Colors.WARNING}⚠ Gmail not authenticated. Run 'python auth.py' first.{Colors.RESET}")
            print(f"{Colors.DIM}   Falling back to dummy data.{Colors.RESET}\n")
            return self._get_fallback_data()
        
        print(f"{Colors.INFO}📧 Fetching real data from Gmail...{Colors.RESET}")
        
        data = {
            "email_ids": [],
            "thread_ids": [],
            "senders": [],
            "subjects": [],
            "labels": [],
            "unread_ids": [],
            "starred_ids": [],
        }
        
        try:
            emails = get_emails(limit=self.max_emails)
            for email in emails:
                data["email_ids"].append(email.get("id"))
                data["thread_ids"].append(email.get("thread_id"))
                data["subjects"].append(email.get("subject", "No Subject"))
                
                from_header = email.get("from", "")
                if "<" in from_header:
                    sender = from_header.split("<")[1].split(">")[0]
                else:
                    sender = from_header
                if sender and sender not in data["senders"]:
                    data["senders"].append(sender)
            
            try:
                unread = get_unread_emails()
                data["unread_ids"] = [e.get("id") for e in unread[:5]]
            except Exception:
                pass
            
            try:
                starred = get_starred_emails()
                data["starred_ids"] = [e.get("id") for e in starred[:5]]
            except Exception:
                pass
            
            try:
                service = authenticate_gmail()
                labels_result = gmail_call(
                    lambda: service.users().labels().list(userId='me').execute()
                )
                all_labels = labels_result.get('labels', [])
                user_labels = [l['name'] for l in all_labels 
                              if not l['id'].startswith('CATEGORY_') 
                              and l['name'] not in ('INBOX', 'SENT', 'TRASH', 'DRAFT', 'SPAM', 'UNREAD', 'STARRED', 'IMPORTANT')]
                data["labels"] = user_labels[:10] or ['Work', 'Personal', 'Important']
            except Exception:
                data["labels"] = ['Work', 'Personal', 'Important', 'Archive']
            
            self._data = data
            self._print_summary(data)
            return data
            
        except Exception as e:
            print(f"{Colors.ERROR}✗ Error fetching Gmail data: {e}{Colors.RESET}")
            return self._get_fallback_data()
    
    def _get_fallback_data(self) -> Dict[str, Any]:
        return {
            "email_ids": ["msg_001", "msg_002", "msg_003", "msg_004", "msg_005"],
            "thread_ids": ["thread_001", "thread_002", "thread_003"],
            "senders": ["test@example.com", "demo@gmail.com", "user@domain.com"],
            "subjects": ["Test Email", "Demo Subject", "Hello"],
            "labels": ["Work", "Personal", "Important"],
            "unread_ids": ["msg_001"],
            "starred_ids": ["msg_002"],
        }
    
    def _print_summary(self, data: Dict[str, Any]) -> None:
        print(f"  {Colors.SUCCESS}✓ Found:{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['email_ids'])} emails{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['thread_ids'])} threads{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['senders'])} unique senders{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['labels'])} labels{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['unread_ids'])} unread{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['starred_ids'])} starred{Colors.RESET}")
    
    def get_email_id(self, index: int = 0) -> str:
        ids = self._data.get("email_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"msg_{index:03d}"
    
    def get_thread_id(self, index: int = 0) -> str:
        ids = self._data.get("thread_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"thread_{index:03d}"
    
    def get_sender(self, index: int = 0) -> str:
        senders = self._data.get("senders", []) if self._data else []
        if senders and index < len(senders):
            return senders[index]
        return "example@domain.com"
    
    def get_label(self, index: int = 0) -> str:
        labels = self._data.get("labels", []) if self._data else []
        if labels and index < len(labels):
            return labels[index]
        return "Important"


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic Query Generator
# ═══════════════════════════════════════════════════════════════════════════════

class QueryGenerator:
    """Generates realistic test queries using real Gmail data."""
    
    def __init__(self, data_fetcher: GmailDataFetcher):
        self.data = data_fetcher
    
    def _get_ids(self, count: int = 3) -> List[str]:
        return [self.data.get_email_id(i) for i in range(min(count, 10))]
    
    def _get_thread_ids(self, count: int = 3) -> List[str]:
        return [self.data.get_thread_id(i) for i in range(min(count, 10))]
    
    def _get_senders(self, count: int = 3) -> List[str]:
        return [self.data.get_sender(i) for i in range(min(count, 10))]
    
    def _get_labels(self, count: int = 3) -> List[str]:
        return [self.data.get_label(i) for i in range(min(count, 10))]
    
    def generate(self, tool: str) -> List[str]:
        """Generate 8-10 realistic queries for the specified tool."""
        method = getattr(self, f"_gen_{tool}", self._gen_default)
        return method()
    
    def _gen_authenticate_gmail(self) -> List[str]:
        return [
            "Connect to my Gmail account", "Authenticate with Gmail",
            "Sign in to Gmail", "Verify my Gmail connection",
            "Check Gmail authentication status", "Link my Gmail",
            "Authorize Gmail access", "Set up Gmail connection",
            "Initialize Gmail service", "Verify Gmail credentials",
        ]
    
    def _gen_get_emails(self) -> List[str]:
        return [
            "Show my recent emails", "Read my inbox", "Display my emails",
            "What emails do I have?", "Check my messages", "List my emails",
            "Get my inbox", "Show me my mail", "Fetch my emails", "View my inbox",
        ]
    
    def _gen_get_email_by_id(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Show email {ids[0]}",
            f"Read message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Get email with ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Open email {ids[0]}",
            f"View message ID {ids[1] if len(ids) > 1 else ids[0]}",
            f"Fetch email details for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Display email {ids[0]}",
            f"Get details of email {ids[0]}",
            f"Read email with ID ending in {ids[0][-6:]}",
            f"Show me message {ids[0]}",
        ]
    
    def _gen_get_unread_emails(self) -> List[str]:
        return [
            "Show my unread emails", "Check unread messages",
            "What emails haven't I read?", "List unread mail",
            "Display unread emails", "Get my unread messages",
            "Show unread inbox items", "Fetch unread emails",
            "Any new emails I haven't seen?", "Check my unread",
        ]
    
    def _gen_get_starred_emails(self) -> List[str]:
        return [
            "Show my starred emails", "Check important messages",
            "Display starred items", "Get my flagged emails",
            "Show important emails", "List starred messages",
            "What did I star?", "Fetch starred emails",
            "View my starred mail", "Show my favorites",
        ]
    
    def _gen_search_emails(self) -> List[str]:
        subjects = self.data._data.get("subjects", []) if self.data._data else []
        terms = subjects[:3] if subjects else ["invoice", "meeting", "update"]
        return [
            f"Search emails about {terms[0]}",
            f"Find messages containing '{terms[1] if len(terms) > 1 else 'report'}'",
            "Search for emails with subject 'meeting'",
            "Look for emails about budget",
            "Find all emails regarding the conference",
            "Search my mail for 'contract'",
            "Find emails with 'urgent' in them",
            "Search for messages about the deadline",
            "Look for emails from last month about sales",
            "Find emails containing 'Q4 report'",
        ]
    
    def _gen_get_emails_by_sender(self) -> List[str]:
        senders = self._get_senders(5)
        return [
            f"Show emails from {senders[0]}",
            f"Get messages from {senders[1] if len(senders) > 1 else senders[0]}",
            f"Find emails sent by {senders[2] if len(senders) > 2 else senders[0]}",
            f"Show mail from {senders[0]}",
            f"Get all emails from {senders[1] if len(senders) > 1 else senders[0]}",
            f"Find messages from {senders[2] if len(senders) > 2 else senders[0]}",
            f"Show emails from {senders[0]}",
            f"Get mail sent by {senders[1] if len(senders) > 1 else senders[0]}",
            f"Find emails from {senders[2] if len(senders) > 2 else senders[0]}",
            f"Show messages from {senders[0]}",
        ]
    
    def _gen_get_emails_by_label(self) -> List[str]:
        labels = self._get_labels(5)
        return [
            f"Show emails labeled '{labels[0]}'",
            f"Get messages with label '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Find emails tagged '{labels[2] if len(labels) > 2 else labels[0]}'",
            f"Show mail labeled '{labels[0]}'",
            f"Get emails with '{labels[1] if len(labels) > 1 else labels[0]}' label",
            f"Find messages tagged '{labels[2] if len(labels) > 2 else labels[0]}'",
            f"Show emails in label '{labels[0]}'",
            f"Get mail with '{labels[1] if len(labels) > 1 else labels[0]}' label",
            f"Find emails labeled '{labels[2] if len(labels) > 2 else labels[0]}'",
            f"Show messages tagged '{labels[0]}'",
        ]
    
    def _gen_get_emails_by_date_range(self) -> List[str]:
        return [
            "Show emails from January 1 to January 31",
            "Get messages between 2024/01/01 and 2024/01/31",
            "Find emails from last week",
            "Show mail from last month",
            "Get emails between March 15 and April 15",
            "Find messages from past month",
            "Show emails from 2024/06/01 to 2024/06/30",
            "Get mail between yesterday and today",
            "Find emails from past week",
            "Show messages from date range 2024-01-01 to 2024-12-31",
        ]
    
    def _gen_get_email_thread(self) -> List[str]:
        tids = self._get_thread_ids(5)
        return [
            f"Show the conversation thread {tids[0]}",
            f"Get email thread with ID {tids[1] if len(tids) > 1 else tids[0]}",
            f"View thread {tids[2] if len(tids) > 2 else tids[0]}",
            f"Display conversation {tids[0]}",
            f"Show thread for message {tids[1] if len(tids) > 1 else tids[0]}",
            f"Get thread ID {tids[2] if len(tids) > 2 else tids[0]}",
            f"View conversation thread {tids[0]}",
            f"Show thread {tids[1] if len(tids) > 1 else tids[0]}",
            f"Get email thread {tids[2] if len(tids) > 2 else tids[0]}",
            f"Display thread {tids[0]}",
        ]
    
    def _gen_send_email(self) -> List[str]:
        senders = self._get_senders(3)
        return [
            f"Send an email to {senders[0]} saying I'll be late",
            "Mail my manager about the meeting delay",
            f"Write and send a quick update to {senders[1] if len(senders) > 1 else senders[0]}",
            f"Send message to {senders[0]}: Thanks for your help!",
            f"Email {senders[2] if len(senders) > 2 else senders[0]} about the project status",
            "Send a note to support@service.io requesting assistance",
            f"Write email to {senders[0]} about vacation days",
            f"Send quick message to {senders[1] if len(senders) > 1 else senders[0]} confirming our call",
            f"Email {senders[2] if len(senders) > 2 else senders[0]} asking for access",
            f"Send update to {senders[0]} about the campaign",
        ]
    
    def _gen_send_email_with_attachment(self) -> List[str]:
        senders = self._get_senders(3)
        return [
            f"Send email with attachment to {senders[0]}",
            f"Mail document to {senders[1] if len(senders) > 1 else senders[0]} with file attached",
            f"Send report.pdf to {senders[2] if len(senders) > 2 else senders[0]}",
            f"Email {senders[0]} with the spreadsheet attached",
            f"Send presentation to {senders[1] if len(senders) > 1 else senders[0]} with attachment",
            f"Mail invoice.pdf to {senders[2] if len(senders) > 2 else senders[0]}",
            "Send contract with attachment to legal@company.com",
            f"Email {senders[0]} the photos as attachments",
            f"Send file to {senders[1] if len(senders) > 1 else senders[0]} with my request",
            f"Mail report with document to {senders[2] if len(senders) > 2 else senders[0]}",
        ]
    
    def _gen_draft_email(self) -> List[str]:
        senders = self._get_senders(3)
        return [
            f"Draft an email to {senders[0]} about the proposal",
            f"Create a draft message for {senders[1] if len(senders) > 1 else senders[0]}",
            f"Write draft email to {senders[2] if len(senders) > 2 else senders[0]}",
            "Draft message to my team about the delay",
            f"Compose draft for {senders[0]}",
            "Create email draft to hr@company.com",
            "Draft a note to support@service.io",
            f"Write draft message to {senders[1] if len(senders) > 1 else senders[0]}",
            f"Draft email for {senders[2] if len(senders) > 2 else senders[0]}",
            "Create draft for billing@company.com",
        ]
    
    def _gen_send_draft(self) -> List[str]:
        ids = self._get_ids(3)
        return [
            f"Send draft ID draft_{ids[0]}",
            f"Send my saved draft with ID r{ids[1] if len(ids) > 1 else ids[0]}",
            f"Dispatch draft message {ids[2] if len(ids) > 2 else ids[0]}",
            f"Send the draft {ids[0]}",
            f"Mail out draft ID {ids[1] if len(ids) > 1 else ids[0]}",
            f"Send draft number {ids[2] if len(ids) > 2 else ids[0]}",
            f"Dispatch saved draft {ids[0]}",
            f"Send draft {ids[1] if len(ids) > 1 else ids[0]}",
            f"Mail draft {ids[2] if len(ids) > 2 else ids[0]}",
            f"Send the draft with ID {ids[0]}",
        ]
    
    def _gen_update_draft(self) -> List[str]:
        ids = self._get_ids(3)
        return [
            f"Update draft {ids[0]} with new content",
            f"Edit draft ID {ids[1] if len(ids) > 1 else ids[0]} to add more details",
            f"Modify draft {ids[2] if len(ids) > 2 else ids[0]} with changes",
            f"Update draft {ids[0]} with new text",
            f"Edit draft message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Revise draft ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Update the content of draft {ids[0]}",
            f"Modify draft {ids[1] if len(ids) > 1 else ids[0]}",
            f"Edit draft {ids[2] if len(ids) > 2 else ids[0]}",
            f"Update draft {ids[0]} with corrections",
        ]
    
    def _gen_delete_draft(self) -> List[str]:
        ids = self._get_ids(3)
        return [
            f"Delete draft ID {ids[0]}",
            f"Remove draft {ids[1] if len(ids) > 1 else ids[0]}",
            f"Discard draft {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete draft {ids[0]}",
            f"Remove draft message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Trash draft ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete draft {ids[0]}",
            f"Remove draft {ids[1] if len(ids) > 1 else ids[0]}",
            f"Discard draft {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete the draft with ID {ids[0]}",
        ]
    
    def _gen_reply_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Reply to email {ids[0]} saying thanks",
            f"Respond to message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Reply to {ids[2] if len(ids) > 2 else ids[0]} with confirmation",
            f"Send reply to email {ids[0]}",
            f"Respond to {ids[1] if len(ids) > 1 else ids[0]}",
            f"Reply to message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Answer email {ids[0]}",
            f"Reply to {ids[1] if len(ids) > 1 else ids[0]} with details",
            f"Respond to email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Reply to {ids[0]} saying approved",
        ]
    
    def _gen_reply_all(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Reply all to email {ids[0]}",
            f"Respond to everyone on message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Reply all for thread {ids[2] if len(ids) > 2 else ids[0]}",
            f"Send reply all to email {ids[0]}",
            f"Respond to all recipients of {ids[1] if len(ids) > 1 else ids[0]}",
            f"Reply all to message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Answer all on email {ids[0]}",
            f"Reply all to {ids[1] if len(ids) > 1 else ids[0]}",
            f"Respond to everyone on email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Reply all for {ids[0]}",
        ]
    
    def _gen_forward_email(self) -> List[str]:
        ids = self._get_ids(5)
        senders = self._get_senders(5)
        return [
            f"Forward email {ids[0]} to {senders[0]}",
            f"Send message {ids[1] if len(ids) > 1 else ids[0]} to {senders[1] if len(senders) > 1 else senders[0]}",
            f"Forward {ids[2] if len(ids) > 2 else ids[0]} to {senders[2] if len(senders) > 2 else senders[0]}",
            f"Forward email {ids[0]} to {senders[3] if len(senders) > 3 else senders[0]}",
            f"Send {ids[1] if len(ids) > 1 else ids[0]} to {senders[4] if len(senders) > 4 else senders[0]}",
            f"Forward message ID {ids[2] if len(ids) > 2 else ids[0]} to {senders[0]}",
            f"Forward {ids[0]} to {senders[1] if len(senders) > 1 else senders[0]}",
            f"Send {ids[1] if len(ids) > 1 else ids[0]} to {senders[2] if len(senders) > 2 else senders[0]}",
            f"Forward email {ids[2] if len(ids) > 2 else ids[0]} to {senders[0]}",
            f"Forward {ids[0]} to {senders[1] if len(senders) > 1 else senders[0]}",
        ]
    
    def _gen_list_labels(self) -> List[str]:
        return [
            "Show all my Gmail labels", "List my email labels",
            "What labels do I have?", "Display my Gmail tags",
            "Get all my labels", "Show my label list",
            "List available labels", "What labels are in my Gmail?",
            "Show me all labels", "Display available Gmail labels",
        ]
    
    def _gen_add_label(self) -> List[str]:
        ids = self._get_ids(5)
        labels = self._get_labels(5)
        return [
            f"Add label '{labels[0]}' to email {ids[0]}",
            f"Tag message {ids[1] if len(ids) > 1 else ids[0]} with '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Apply label '{labels[2] if len(labels) > 2 else labels[0]}' to {ids[2] if len(ids) > 2 else ids[0]}",
            f"Add label '{labels[0]}' to email {ids[0]}",
            f"Tag {ids[1] if len(ids) > 1 else ids[0]} with '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Apply '{labels[2] if len(labels) > 2 else labels[0]}' label to message {ids[2] if len(ids) > 2 else ids[0]}",
            f"Add '{labels[0]}' label to {ids[0]}",
            f"Tag {ids[1] if len(ids) > 1 else ids[0]} with '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Apply label '{labels[2] if len(labels) > 2 else labels[0]}' to email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Add label '{labels[0]}' to {ids[0]}",
        ]
    
    def _gen_remove_label(self) -> List[str]:
        ids = self._get_ids(5)
        labels = self._get_labels(5)
        return [
            f"Remove label '{labels[0]}' from email {ids[0]}",
            f"Untag message {ids[1] if len(ids) > 1 else ids[0]} from '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Remove '{labels[2] if len(labels) > 2 else labels[0]}' label from {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete label '{labels[0]}' from email {ids[0]}",
            f"Untag {ids[1] if len(ids) > 1 else ids[0]} from '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Remove '{labels[2] if len(labels) > 2 else labels[0]}' label from message {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete '{labels[0]}' label from {ids[0]}",
            f"Untag {ids[1] if len(ids) > 1 else ids[0]} from '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Remove '{labels[2] if len(labels) > 2 else labels[0]}' label from email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete label '{labels[0]}' from {ids[0]}",
        ]
    
    def _gen_create_label(self) -> List[str]:
        return [
            "Create a new label called 'Projects'", "Make label 'Urgent' in Gmail",
            "Add label 'Work-Personal'", "Create label named 'Finance-2024'",
            "Make new label 'Client-A'", "Create 'Receipts-2024' label",
            "Add label 'Travel-Plans'", "Create label 'Important-Archive'",
            "Make label 'Newsletter'", "Create new label 'Bills-To-Pay'",
        ]
    
    def _gen_mark_as_read(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Mark email {ids[0]} as read",
            f"Set message {ids[1] if len(ids) > 1 else ids[0]} to read",
            f"Mark {ids[2] if len(ids) > 2 else ids[0]} as read",
            f"Set email {ids[0]} as read",
            f"Mark {ids[1] if len(ids) > 1 else ids[0]} as read",
            f"Set message ID {ids[2] if len(ids) > 2 else ids[0]} as read",
            f"Mark {ids[0]} as read",
            f"Set {ids[1] if len(ids) > 1 else ids[0]} to read",
            f"Mark email {ids[2] if len(ids) > 2 else ids[0]} as read",
            f"Set {ids[0]} to read",
        ]
    
    def _gen_mark_as_unread(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Mark email {ids[0]} as unread",
            f"Set message {ids[1] if len(ids) > 1 else ids[0]} to unread",
            f"Mark {ids[2] if len(ids) > 2 else ids[0]} as unread",
            f"Set email {ids[0]} as unread",
            f"Mark {ids[1] if len(ids) > 1 else ids[0]} as unread",
            f"Set message ID {ids[2] if len(ids) > 2 else ids[0]} as unread",
            f"Mark {ids[0]} as unread",
            f"Set {ids[1] if len(ids) > 1 else ids[0]} to unread",
            f"Mark email {ids[2] if len(ids) > 2 else ids[0]} as unread",
            f"Set {ids[0]} to unread",
        ]
    
    def _gen_star_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Star email {ids[0]}",
            f"Add star to message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Star {ids[2] if len(ids) > 2 else ids[0]}",
            f"Flag email {ids[0]}",
            f"Star {ids[1] if len(ids) > 1 else ids[0]}",
            f"Add star to message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Star {ids[0]}",
            f"Flag {ids[1] if len(ids) > 1 else ids[0]}",
            f"Star email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Add star to {ids[0]}",
        ]
    
    def _gen_unstar_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Unstar email {ids[0]}",
            f"Remove star from message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Unstar {ids[2] if len(ids) > 2 else ids[0]}",
            f"Remove flag from email {ids[0]}",
            f"Unstar {ids[1] if len(ids) > 1 else ids[0]}",
            f"Remove star from message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Unstar {ids[0]}",
            f"Unflag {ids[1] if len(ids) > 1 else ids[0]}",
            f"Unstar email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Remove star from {ids[0]}",
        ]
    
    def _gen_archive_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Archive email {ids[0]}",
            f"Archive message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Move {ids[2] if len(ids) > 2 else ids[0]} to archive",
            f"Archive email {ids[0]}",
            f"Archive {ids[1] if len(ids) > 1 else ids[0]}",
            f"Archive message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Archive {ids[0]}",
            f"Move {ids[1] if len(ids) > 1 else ids[0]} to archive",
            f"Archive email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Archive {ids[0]}",
        ]
    
    def _gen_unarchive_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Unarchive email {ids[0]}",
            f"Restore message {ids[1] if len(ids) > 1 else ids[0]} to inbox",
            f"Unarchive {ids[2] if len(ids) > 2 else ids[0]}",
            f"Move email {ids[0]} to inbox",
            f"Unarchive {ids[1] if len(ids) > 1 else ids[0]}",
            f"Restore message ID {ids[2] if len(ids) > 2 else ids[0]} to inbox",
            f"Unarchive {ids[0]}",
            f"Move {ids[1] if len(ids) > 1 else ids[0]} to inbox",
            f"Unarchive email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Restore {ids[0]} to inbox",
        ]
    
    def _gen_move_to_folder(self) -> List[str]:
        ids = self._get_ids(5)
        labels = self._get_labels(5)
        return [
            f"Move email {ids[0]} to '{labels[0]}' folder",
            f"Move message {ids[1] if len(ids) > 1 else ids[0]} to folder '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Put {ids[2] if len(ids) > 2 else ids[0]} in '{labels[2] if len(labels) > 2 else labels[0]}' folder",
            f"Move email {ids[0]} to '{labels[0]}'",
            f"Move {ids[1] if len(ids) > 1 else ids[0]} to folder '{labels[1] if len(labels) > 1 else labels[0]}'",
            f"Put message {ids[2] if len(ids) > 2 else ids[0]} in '{labels[2] if len(labels) > 2 else labels[0]}'",
            f"Move {ids[0]} to '{labels[0]}' folder",
            f"Put {ids[1] if len(ids) > 1 else ids[0]} in '{labels[1] if len(labels) > 1 else labels[0]}' folder",
            f"Move email {ids[2] if len(ids) > 2 else ids[0]} to '{labels[2] if len(labels) > 2 else labels[0]}'",
            f"Put {ids[0]} in '{labels[0]}' folder",
        ]
    
    def _gen_trash_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Trash email {ids[0]}",
            f"Delete message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Move {ids[2] if len(ids) > 2 else ids[0]} to trash",
            f"Trash email {ids[0]}",
            f"Delete {ids[1] if len(ids) > 1 else ids[0]}",
            f"Trash message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete {ids[0]}",
            f"Move {ids[1] if len(ids) > 1 else ids[0]} to trash",
            f"Trash email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Delete {ids[0]}",
        ]
    
    def _gen_restore_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Restore email {ids[0]} from trash",
            f"Recover message {ids[1] if len(ids) > 1 else ids[0]} from trash",
            f"Restore {ids[2] if len(ids) > 2 else ids[0]}",
            f"Undelete email {ids[0]}",
            f"Restore {ids[1] if len(ids) > 1 else ids[0]} from trash",
            f"Recover message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Restore {ids[0]}",
            f"Undelete {ids[1] if len(ids) > 1 else ids[0]}",
            f"Restore email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Recover {ids[0]} from trash",
        ]
    
    def _gen_delete_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Permanently delete email {ids[0]}",
            f"Erase message {ids[1] if len(ids) > 1 else ids[0]} forever",
            f"Delete {ids[2] if len(ids) > 2 else ids[0]} permanently",
            f"Erase email {ids[0]}",
            f"Permanently remove {ids[1] if len(ids) > 1 else ids[0]}",
            f"Delete forever message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Erase {ids[0]} permanently",
            f"Permanently delete {ids[1] if len(ids) > 1 else ids[0]}",
            f"Delete forever email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Erase {ids[0]} permanently",
        ]
    
    def _gen_get_attachments(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Show attachments on email {ids[0]}",
            f"List attachments for message {ids[1] if len(ids) > 1 else ids[0]}",
            f"What files are attached to {ids[2] if len(ids) > 2 else ids[0]}?",
            f"Show files on email {ids[0]}",
            f"List attachments for message ID {ids[1] if len(ids) > 1 else ids[0]}",
            f"What attachments does {ids[2] if len(ids) > 2 else ids[0]} have?",
            f"Show attached files on {ids[0]}",
            f"List attachments on email {ids[1] if len(ids) > 1 else ids[0]}",
            f"Get files attached to {ids[2] if len(ids) > 2 else ids[0]}",
        ]
    
    def _gen_download_attachment(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Download attachment from email {ids[0]}",
            f"Get the file attached to message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Download files from {ids[2] if len(ids) > 2 else ids[0]}",
            f"Save attachments from email {ids[0]}",
            f"Download files on message ID {ids[1] if len(ids) > 1 else ids[0]}",
            f"Get attachment for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Download the file from {ids[0]}",
            f"Save attachment from {ids[1] if len(ids) > 1 else ids[0]}",
            f"Download files from email {ids[2] if len(ids) > 2 else ids[0]}",
        ]
    
    def _gen_save_attachment_to_disk(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Save attachment to disk from email {ids[0]}",
            f"Download and save files from message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Save attached file from {ids[2] if len(ids) > 2 else ids[0]} to my computer",
            f"Download attachment to disk from email {ids[0]}",
            f"Save files on message ID {ids[1] if len(ids) > 1 else ids[0]} to downloads",
            f"Download and save attachment for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Save the file from {ids[0]} to my downloads",
            f"Download attachment to disk from {ids[1] if len(ids) > 1 else ids[0]}",
            f"Save attached file from email {ids[2] if len(ids) > 2 else ids[0]}",
        ]
    
    def _gen_schedule_email(self) -> List[str]:
        senders = self._get_senders(3)
        return [
            f"Schedule email to {senders[0]} for tomorrow at 9am",
            f"Send message to {senders[1] if len(senders) > 1 else senders[0]} on Monday morning",
            f"Schedule email to team@company.com for next week",
            f"Queue email to {senders[2] if len(senders) > 2 else senders[0]} for Friday afternoon",
            f"Schedule message to {senders[0]} for next week",
            f"Send email to hr@company.com on January 1st",
            f"Schedule message to support@service.io for 3pm today",
            f"Queue email to {senders[1] if len(senders) > 1 else senders[0]} for next month",
            f"Schedule email to {senders[2] if len(senders) > 2 else senders[0]} for tomorrow evening",
            f"Send message to billing@company.com on the first of next month",
        ]
    
    def _gen_set_email_reminder(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Set reminder for email {ids[0]} tomorrow",
            f"Remind me about message {ids[1] if len(ids) > 1 else ids[0]} in 2 hours",
            f"Create reminder for {ids[2] if len(ids) > 2 else ids[0]} next week",
            f"Set email reminder for {ids[0]} on Friday",
            f"Remind me about message ID {ids[1] if len(ids) > 1 else ids[0]} in 3 days",
            f"Add reminder for {ids[2] if len(ids) > 2 else ids[0]} at 5pm today",
            f"Set reminder for {ids[0]} tomorrow morning",
            f"Remind me about email {ids[1] if len(ids) > 1 else ids[0]} next Monday",
            f"Create reminder for {ids[2] if len(ids) > 2 else ids[0]} in 1 week",
            f"Set email reminder for {ids[0]} at 10am",
        ]
    
    def _gen_confirm_action(self) -> List[str]:
        return [
            "Confirm delete action on email", "Approve sending this message",
            "Confirm trash operation", "Approve archive action",
            "Confirm email deletion", "Approve label change",
            "Confirm draft deletion", "Approve forward action",
            "Confirm reply to all", "Approve bulk delete",
        ]
    
    def _gen_validate_email_address(self) -> List[str]:
        senders = self._get_senders(10)
        test_emails = senders + ["test@example.com", "invalid.email", "user@domain.com"]
        return [
            f"Check if {test_emails[0]} is valid",
            f"Validate email address {test_emails[1]}",
            f"Is {test_emails[2]} a proper email?",
            f"Verify {test_emails[3]} format",
            f"Check email validation for {test_emails[4]}",
            f"Validate {test_emails[5]}",
            f"Is {test_emails[6]} valid?",
            f"Check {test_emails[7]} format",
            f"Validate {test_emails[8]}",
            f"Is {test_emails[9]} a real email?",
        ]
    
    def _gen_sanitize_email_content(self) -> List[str]:
        return [
            "Clean this email content: <script>alert('xss')</script>",
            "Sanitize message body with HTML tags",
            "Clean email text for sending", "Sanitize content for safe display",
            "Clean this message body", "Sanitize email content with special characters",
            "Clean text for email composition", "Sanitize body content",
            "Clean email message text", "Sanitize content before sending",
        ]
    
    def _gen_log_email_action(self) -> List[str]:
        return [
            "Log this email action to audit trail",
            "Record email deletion in audit log",
            "Log sent message for compliance",
            "Record this email operation",
            "Add email action to log",
            "Log archive action for tracking",
            "Record email send operation",
            "Log trash action in audit",
            "Add this action to email log",
            "Record email modification",
        ]
    
    def _gen_audit_email_history(self) -> List[str]:
        return [
            "Show my email audit history", "Get audit log of email actions",
            "View email action history", "Display audit trail for emails",
            "Show logged email operations", "Get history of email actions",
            "View email audit records", "Display email action log",
            "Show email audit trail", "Get audit history of messages",
        ]
    
    def _gen_count_emails_by_sender(self) -> List[str]:
        return [
            "Count emails from each sender", "How many emails per contact?",
            "Show email count by sender", "Get statistics on email senders",
            "Count messages from each person", "Email distribution by sender",
            "How many from each email address?", "Sender email count statistics",
            "Count my emails per contact", "Email frequency by sender",
        ]
    
    def _gen_email_activity_summary(self) -> List[str]:
        return [
            "Show my email activity summary", "Get email statistics overview",
            "Display email usage summary", "What are my email activity stats?",
            "Show email analytics summary", "Get overview of email activity",
            "Display email statistics", "Show email usage report",
            "Get email activity overview", "Display email summary stats",
        ]
    
    def _gen_most_frequent_contacts(self) -> List[str]:
        return [
            "Who are my most frequent contacts?", "Show top email contacts",
            "Who emails me the most?", "List my most active contacts",
            "Show frequent email senders", "Get my top email contacts",
            "Who are my main contacts?", "Show most common email senders",
            "List frequent email partners", "Get top contact list",
        ]
    
    def _gen_summarize_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Summarize email {ids[0]}",
            f"Give me a summary of message {ids[1] if len(ids) > 1 else ids[0]}",
            f"TL;DR for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Summarize email {ids[0]}",
            f"Brief summary of {ids[1] if len(ids) > 1 else ids[0]}",
            f"Summarize message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get summary of {ids[0]}",
            f"Summarize {ids[1] if len(ids) > 1 else ids[0]} for me",
            f"Brief version of email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Summarize {ids[0]}",
        ]
    
    def _gen_summarize_emails(self) -> List[str]:
        return [
            "Summarize my recent emails", "Give me TL;DR of my inbox",
            "Summarize last 10 messages", "Brief summary of unread emails",
            "Summarize today's emails", "Quick summary of my mail",
            "Summarize this week's messages", "Give overview of my emails",
            "Summarize important emails", "Brief digest of my inbox",
        ]
    
    def _gen_classify_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Classify email {ids[0]}",
            f"What category is message {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Classify {ids[2] if len(ids) > 2 else ids[0]}",
            f"What type is email {ids[0]}?",
            f"Classify {ids[1] if len(ids) > 1 else ids[0]}",
            f"Categorize message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"What category is {ids[0]}?",
            f"Classify {ids[1] if len(ids) > 1 else ids[0]}",
            f"Determine category of email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Classify {ids[0]}",
        ]
    
    def _gen_detect_urgency(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Check urgency of email {ids[0]}",
            f"Is message {ids[1] if len(ids) > 1 else ids[0]} urgent?",
            f"How urgent is {ids[2] if len(ids) > 2 else ids[0]}?",
            f"Check if email {ids[0]} is urgent",
            f"Check if {ids[1] if len(ids) > 1 else ids[0]} is urgent",
            f"Is message ID {ids[2] if len(ids) > 2 else ids[0]} time-sensitive?",
            f"Check urgency of {ids[0]}",
            f"How critical is {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Detect urgency for email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Is {ids[0]} urgent?",
        ]
    
    def _gen_detect_action_required(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Does email {ids[0]} need action?",
            f"Check if message {ids[1] if len(ids) > 1 else ids[0]} requires response",
            f"What action needed for {ids[2] if len(ids) > 2 else ids[0]}?",
            f"Detect action items in email {ids[0]}",
            f"Check {ids[1] if len(ids) > 1 else ids[0]} for action required",
            f"Does message ID {ids[2] if len(ids) > 2 else ids[0]} need follow-up?",
            f"What should I do about {ids[0]}?",
            f"Check {ids[1] if len(ids) > 1 else ids[0]} for action items",
            f"Detect required actions for email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Does {ids[0]} need my attention?",
        ]
    
    def _gen_sentiment_analysis(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Analyze sentiment of email {ids[0]}",
            f"What tone does message {ids[1] if len(ids) > 1 else ids[0]} have?",
            f"Check sentiment of {ids[2] if len(ids) > 2 else ids[0]}",
            f"Is email {ids[0]} positive or negative?",
            f"Sentiment of {ids[1] if len(ids) > 1 else ids[0]}",
            f"Analyze tone of message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"What sentiment is {ids[0]}?",
            f"Check {ids[1] if len(ids) > 1 else ids[0]} sentiment",
            f"Analyze email {ids[2] if len(ids) > 2 else ids[0]} tone",
            f"Sentiment of {ids[0]}",
        ]
    
    def _gen_extract_tasks(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Extract tasks from email {ids[0]}",
            f"What to-dos are in message {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Get action items from {ids[2] if len(ids) > 2 else ids[0]}",
            f"Extract tasks from email {ids[0]}",
            f"What tasks in {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Extract to-dos from message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get tasks from {ids[0]}",
            f"What action items in {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Extract tasks from email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get to-do list from {ids[0]}",
        ]
    
    def _gen_extract_dates(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Extract dates from email {ids[0]}",
            f"What deadlines in message {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Find dates mentioned in {ids[2] if len(ids) > 2 else ids[0]}",
            f"Extract dates from email {ids[0]}",
            f"What dates in {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Find dates in message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get dates from {ids[0]}",
            f"Extract deadlines from {ids[1] if len(ids) > 1 else ids[0]}",
            f"What dates mentioned in email {ids[2] if len(ids) > 2 else ids[0]}?",
            f"Find dates in {ids[0]}",
        ]
    
    def _gen_extract_contacts(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Extract contacts from email {ids[0]}",
            f"What contact info in message {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Find phone numbers in {ids[2] if len(ids) > 2 else ids[0]}",
            f"Extract contacts from email {ids[0]}",
            f"What people mentioned in {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Find contact details in message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get contacts from {ids[0]}",
            f"Extract people from {ids[1] if len(ids) > 1 else ids[0]}",
            f"What contacts in email {ids[2] if len(ids) > 2 else ids[0]}?",
            f"Find names and emails in {ids[0]}",
        ]
    
    def _gen_extract_links(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Extract links from email {ids[0]}",
            f"What URLs in message {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Find links in {ids[2] if len(ids) > 2 else ids[0]}",
            f"Extract URLs from email {ids[0]}",
            f"What web links in {ids[1] if len(ids) > 1 else ids[0]}?",
            f"Find all links in message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get URLs from {ids[0]}",
            f"Extract links from {ids[1] if len(ids) > 1 else ids[0]}",
            f"What links in email {ids[2] if len(ids) > 2 else ids[0]}?",
            f"Find web addresses in {ids[0]}",
        ]
    
    def _gen_draft_reply(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Draft a reply to email {ids[0]}",
            f"Create response draft for message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write reply draft for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Draft response to email {ids[0]}",
            f"Create reply for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write draft response for message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Draft reply to {ids[0]}",
            f"Create response draft for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write reply for email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Draft response to {ids[0]}",
        ]
    
    def _gen_generate_followup(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Generate follow-up for email {ids[0]}",
            f"Create follow-up message for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write follow-up for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate follow-up to email {ids[0]}",
            f"Create chase-up for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write follow-up for message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate follow-up to {ids[0]}",
            f"Create follow-up for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write chase-up for email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate follow-up to {ids[0]}",
        ]
    
    def _gen_auto_reply(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Generate auto-reply for email {ids[0]}",
            f"Create auto-response for message {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write auto-reply for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate auto-response to email {ids[0]}",
            f"Create auto-reply for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write auto-response for message ID {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate auto-reply to {ids[0]}",
            f"Create auto-response for {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write auto-reply for email {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate auto-response to {ids[0]}",
        ]
    
    def _gen_rewrite_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Rewrite email {ids[0]} professionally",
            f"Make message {ids[1] if len(ids) > 1 else ids[0]} more formal",
            f"Rewrite {ids[2] if len(ids) > 2 else ids[0]} casually",
            f"Make email {ids[0]} friendlier",
            f"Rewrite {ids[1] if len(ids) > 1 else ids[0]} concisely",
            f"Make message ID {ids[2] if len(ids) > 2 else ids[0]} more polite",
            f"Rewrite {ids[0]} professionally",
            f"Make {ids[1] if len(ids) > 1 else ids[0]} more assertive",
            f"Rewrite email {ids[2] if len(ids) > 2 else ids[0]} in simpler language",
            f"Make {ids[0]} more formal",
        ]
    
    def _gen_translate_email(self) -> List[str]:
        ids = self._get_ids(5)
        return [
            f"Translate email {ids[0]} to Spanish",
            f"Convert message {ids[1] if len(ids) > 1 else ids[0]} to French",
            f"Translate {ids[2] if len(ids) > 2 else ids[0]} to German",
            f"Convert email {ids[0]} to Japanese",
            f"Translate {ids[1] if len(ids) > 1 else ids[0]} to Chinese",
            f"Convert message ID {ids[2] if len(ids) > 2 else ids[0]} to Italian",
            f"Translate {ids[0]} to Portuguese",
            f"Convert {ids[1] if len(ids) > 1 else ids[0]} to Russian",
            f"Translate email {ids[2] if len(ids) > 2 else ids[0]} to Arabic",
            f"Convert {ids[0]} to Hindi",
        ]
    
    def _gen_auto_label_emails(self) -> List[str]:
        return [
            "Auto-label my unread emails", "Suggest labels for recent messages",
            "Automatically categorize my inbox", "Auto-label emails from last week",
            "Suggest tags for unread mail", "Auto-categorize my messages",
            "Label my recent emails automatically", "Suggest labels for inbox items",
            "Auto-tag unread emails", "Automatically label my mail",
        ]
    
    def _gen_auto_archive_promotions(self) -> List[str]:
        return [
            "Auto-archive promotional emails", "Automatically archive marketing messages",
            "Archive promo emails automatically", "Auto-archive newsletter emails",
            "Automatically clean up promotions", "Auto-archive spam-like emails",
            "Archive marketing mail automatically", "Auto-archive bulk emails",
            "Automatically archive ads", "Auto-archive commercial emails",
        ]
    
    def _gen_auto_reply_rules(self) -> List[str]:
        return [
            "Suggest auto-reply rules for my inbox", "Create auto-reply suggestions",
            "What auto-replies should I set up?", "Suggest automatic response rules",
            "Generate auto-reply recommendations", "Create rules for auto-responses",
            "Suggest vacation auto-reply setup", "What auto-reply rules would help?",
            "Generate auto-response suggestions", "Suggest inbox auto-reply configuration",
        ]
    
    def _gen_default(self) -> List[str]:
        return [
            f"Execute {self.__class__.__name__}",
            f"Run {self.__class__.__name__} tool",
            f"Test {self.__class__.__name__} functionality",
        ]


# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TestResult:
    tool: str
    category: str
    query: str
    status: str
    selected_tool: Optional[str] = None
    response: str = ""
    error: Optional[str] = None
    execution_time_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    validation_passed: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ToolSummary:
    tool: str
    category: str
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    timeout: int = 0
    error: int = 0
    avg_execution_time_ms: float = 0.0
    
    @property
    def success_rate(self) -> float:
        if self.total_tests == 0:
            return 0.0
        return (self.passed / self.total_tests) * 100


@dataclass
class TestReport:
    start_time: str
    end_time: str
    total_tools: int = 0
    total_tests: int = 0
    total_passed: int = 0
    total_failed: int = 0
    tool_summaries: List[ToolSummary] = field(default_factory=list)
    results: List[TestResult] = field(default_factory=list)
    
    @property
    def overall_success_rate(self) -> float:
        if self.total_tests == 0:
            return 0.0
        return (self.total_passed / self.total_tests) * 100
    
    @property
    def duration_seconds(self) -> float:
        start = datetime.fromisoformat(self.start_time)
        end = datetime.fromisoformat(self.end_time)
        return (end - start).total_seconds()


# ═══════════════════════════════════════════════════════════════════════════════
# Gmail Tool Categories
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_CATEGORIES = {
    "authenticate_gmail": ToolCategory.CORE,
    "get_emails": ToolCategory.CORE,
    "get_email_by_id": ToolCategory.CORE,
    "get_unread_emails": ToolCategory.CORE,
    "get_starred_emails": ToolCategory.CORE,
    "search_emails": ToolCategory.CORE,
    "get_emails_by_sender": ToolCategory.CORE,
    "get_emails_by_label": ToolCategory.CORE,
    "get_emails_by_date_range": ToolCategory.CORE,
    "get_email_thread": ToolCategory.CORE,
    "send_email": ToolCategory.SEND,
    "send_email_with_attachment": ToolCategory.SEND,
    "draft_email": ToolCategory.SEND,
    "send_draft": ToolCategory.SEND,
    "update_draft": ToolCategory.SEND,
    "delete_draft": ToolCategory.SEND,
    "reply_email": ToolCategory.SEND,
    "reply_all": ToolCategory.SEND,
    "forward_email": ToolCategory.SEND,
    "list_labels": ToolCategory.LABELS,
    "add_label": ToolCategory.LABELS,
    "remove_label": ToolCategory.LABELS,
    "create_label": ToolCategory.LABELS,
    "mark_as_read": ToolCategory.LABELS,
    "mark_as_unread": ToolCategory.LABELS,
    "star_email": ToolCategory.LABELS,
    "unstar_email": ToolCategory.LABELS,
    "archive_email": ToolCategory.LABELS,
    "unarchive_email": ToolCategory.LABELS,
    "move_to_folder": ToolCategory.LABELS,
    "trash_email": ToolCategory.LABELS,
    "restore_email": ToolCategory.LABELS,
    "delete_email": ToolCategory.LABELS,
    "get_attachments": ToolCategory.ATTACHMENTS,
    "download_attachment": ToolCategory.ATTACHMENTS,
    "save_attachment_to_disk": ToolCategory.ATTACHMENTS,
    "schedule_email": ToolCategory.SCHEDULING,
    "set_email_reminder": ToolCategory.SCHEDULING,
    "confirm_action": ToolCategory.SECURITY,
    "validate_email_address": ToolCategory.SECURITY,
    "sanitize_email_content": ToolCategory.SECURITY,
    "log_email_action": ToolCategory.SECURITY,
    "audit_email_history": ToolCategory.SECURITY,
    "count_emails_by_sender": ToolCategory.ANALYTICS,
    "email_activity_summary": ToolCategory.ANALYTICS,
    "most_frequent_contacts": ToolCategory.ANALYTICS,
    "summarize_email": ToolCategory.AI,
    "summarize_emails": ToolCategory.AI,
    "classify_email": ToolCategory.AI,
    "detect_urgency": ToolCategory.AI,
    "detect_action_required": ToolCategory.AI,
    "sentiment_analysis": ToolCategory.AI,
    "extract_tasks": ToolCategory.AI,
    "extract_dates": ToolCategory.AI,
    "extract_contacts": ToolCategory.AI,
    "extract_links": ToolCategory.AI,
    "draft_reply": ToolCategory.AI,
    "generate_followup": ToolCategory.AI,
    "auto_reply": ToolCategory.AI,
    "rewrite_email": ToolCategory.AI,
    "translate_email": ToolCategory.AI,
    "auto_label_emails": ToolCategory.AI,
    "auto_archive_promotions": ToolCategory.AI,
    "auto_reply_rules": ToolCategory.AI,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Tool Monitor & Test Runner
# ═══════════════════════════════════════════════════════════════════════════════

class ToolMonitor:
    def __init__(self, mcp_server):
        self.mcp = mcp_server
        self.last_tool: Optional[str] = None
        self.last_result: Optional[Any] = None
    
    def execute_tool(self, name: str, args: Optional[Dict] = None) -> Any:
        self.last_tool = name
        self.last_result = self.mcp.execute_tool(name, args)
        return self.last_result
    
    def reset(self) -> None:
        self.last_tool = None
        self.last_result = None


class GmailToolTester:
    DEFAULT_TIMEOUT = 30
    
    def __init__(self, verbose: bool = False, timeout: int = DEFAULT_TIMEOUT, max_emails: int = 20):
        self.verbose = verbose
        self.timeout = timeout
        self.results: List[TestResult] = []
        self.report = TestReport(
            start_time=datetime.now().isoformat(),
            end_time=""
        )
        
        # Initialize data fetcher and query generator
        self.data_fetcher = GmailDataFetcher(max_emails=max_emails)
        self.query_generator = QueryGenerator(self.data_fetcher)
        
        # Initialize MCP server and monitor
        self.mcp = build_server()
        self.monitor = ToolMonitor(self.mcp)
        self.state: Dict[str, Any] = {}
        
        # Fetch real data at startup
        self.data_fetcher.fetch()
    
    def _detect_tool(self, response: str) -> Optional[str]:
        import re
        patterns = [
            r'Tool result \(([^)]+)\)',
            r'(\w+)\s*(?:sent|done|complete|created|moved|deleted|updated)',
        ]
        for pattern in patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                return match.group(1)
        return None
    
    def run_single_test(self, tool: str, query: str) -> TestResult:
        start_time = time.time()
        category = TOOL_CATEGORIES.get(tool, ToolCategory.CORE)
        self.state = {}
        self.monitor.reset()
        
        try:
            response = run_agent(query, self.monitor, self.state, mode="gmail")
            execution_time = (time.time() - start_time) * 1000
            selected_tool = self.monitor.last_tool or self._detect_tool(response)
            
            status = "failure" if "error" in response.lower() or "failed" in response.lower() else "success"
            
            result = TestResult(
                tool=tool,
                category=category.value,
                query=query,
                status=status,
                selected_tool=selected_tool,
                response=response[:500] if len(response) > 500 else response,
                execution_time_ms=execution_time
            )
            result.validation_passed = len(response) > 10 and status == "success"
            
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            result = TestResult(
                tool=tool,
                category=category.value,
                query=query,
                status="error",
                error=f"{type(e).__name__}: {str(e)}",
                execution_time_ms=execution_time
            )
        
        return result
    
    def run_tool_tests(self, tool: str, random_select: Optional[int] = None) -> List[TestResult]:
        queries = self.query_generator.generate(tool)
        
        if random_select and random_select < len(queries):
            queries = random.sample(queries, random_select)
        
        results = []
        for query in queries:
            result = self.run_single_test(tool, query)
            results.append(result)
            
            status_icon = "✓" if result.status == "success" else "✗"
            color = Colors.SUCCESS if result.status == "success" else Colors.ERROR
            print(f"    {color}{status_icon} {Colors.DIM}{result.execution_time_ms:.0f}ms{Colors.RESET} {query[:50]}...")
            
            if result.error and self.verbose:
                print(f"    {Colors.ERROR}Error: {result.error[:100]}{Colors.RESET}")
        
        return results
    
    def run_all_tests(self, specific_tool: Optional[str] = None,
                      specific_category: Optional[str] = None,
                      random_count: Optional[int] = None) -> TestReport:
        print(f"\n{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'GMAIL TOOLS TEST SUITE (REAL DATA)'.center(70)}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}\n")
        
        # Determine which tools to test
        available_tools = [t for t in TOOL_CATEGORIES.keys() if t in GMAIL_TOOLS]
        
        if specific_tool:
            tools_to_test = [specific_tool] if specific_tool in available_tools else []
        elif specific_category:
            cat = None
            for c in ToolCategory:
                if c.name.lower() == specific_category.lower() or c.value.lower() == specific_category.lower():
                    cat = c
                    break
            tools_to_test = [t for t, c in TOOL_CATEGORIES.items() if c == cat and t in available_tools]
        else:
            tools_to_test = available_tools
        
        self.report.total_tools = len(tools_to_test)
        
        # Group by category
        by_category: Dict[ToolCategory, List[str]] = {}
        for tool in tools_to_test:
            cat = TOOL_CATEGORIES.get(tool, ToolCategory.CORE)
            by_category.setdefault(cat, []).append(tool)
        
        # Run tests
        tested = 0
        for category, tools in sorted(by_category.items(), key=lambda x: x[0].value):
            color = CATEGORY_COLORS.get(category, Colors.INFO)
            print(f"\n{color}{Colors.BOLD}📁 {category.value}{Colors.RESET}")
            print(f"{color}{'─' * (len(category.value) + 3)}{Colors.RESET}")
            
            for tool in sorted(tools):
                tested += 1
                print(f"\n  {color}▶ {tool} {Colors.DIM}[{tested}/{len(tools_to_test)}]{Colors.RESET}")
                results = self.run_tool_tests(tool, random_count)
                self.results.extend(results)
        
        self._generate_summary()
        return self.report
    
    def _generate_summary(self) -> None:
        self.report.end_time = datetime.now().isoformat()
        self.report.results = self.results
        self.report.total_tests = len(self.results)
        self.report.total_passed = sum(1 for r in self.results if r.status == "success")
        self.report.total_failed = self.report.total_tests - self.report.total_passed
        
        tool_stats: Dict[str, ToolSummary] = {}
        for result in self.results:
            if result.tool not in tool_stats:
                tool_stats[result.tool] = ToolSummary(tool=result.tool, category=result.category)
            s = tool_stats[result.tool]
            s.total_tests += 1
            if result.status == "success":
                s.passed += 1
            elif result.status == "failure":
                s.failed += 1
            elif result.status == "timeout":
                s.timeout += 1
            elif result.status == "error":
                s.error += 1
            s.avg_execution_time_ms += result.execution_time_ms
        
        for s in tool_stats.values():
            if s.total_tests > 0:
                s.avg_execution_time_ms /= s.total_tests
        
        self.report.tool_summaries = list(tool_stats.values())
    
    def print_summary(self) -> None:
        print(f"\n{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'TEST SUMMARY'.center(70)}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}\n")
        
        rate = self.report.overall_success_rate
        rate_color = Colors.SUCCESS if rate >= 80 else Colors.WARNING if rate >= 50 else Colors.ERROR
        
        print(f"  {Colors.BOLD}{'Total Tools Tested:':<30}{Colors.RESET} {self.report.total_tools}")
        print(f"  {Colors.BOLD}{'Total Tests Run:':<30}{Colors.RESET} {self.report.total_tests}")
        print(f"  {Colors.BOLD}{'Passed:':<30}{Colors.RESET} {Colors.SUCCESS}{self.report.total_passed}{Colors.RESET}")
        print(f"  {Colors.BOLD}{'Failed:':<30}{Colors.RESET} {Colors.ERROR}{self.report.total_failed}{Colors.RESET}")
        print(f"  {Colors.BOLD}{'Success Rate:':<30}{Colors.RESET} {rate_color}{rate:.1f}%{Colors.RESET}")
        print(f"  {Colors.BOLD}{'Duration:':<30}{Colors.RESET} {self.report.duration_seconds:.1f}s")
        
        if self.verbose and self.report.tool_summaries:
            print(f"\n{Colors.BOLD}Per-Tool Results:{Colors.RESET}")
            for s in sorted(self.report.tool_summaries, key=lambda x: x.success_rate):
                color = Colors.SUCCESS if s.success_rate >= 80 else Colors.WARNING if s.success_rate >= 50 else Colors.ERROR
                print(f"  {color}{s.tool:.<40} {s.success_rate:>5.1f}% ({s.passed}/{s.total_tests}){Colors.RESET}")
    
    def save_results(self, output_dir: Optional[Path] = None) -> tuple[Path, Path]:
        if output_dir is None:
            output_dir = PROJECT_ROOT / "test_results"
        output_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        json_path = output_dir / f"test_results_{ts}.json"
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump({
                "report": {
                    "start_time": self.report.start_time,
                    "end_time": self.report.end_time,
                    "total_tools": self.report.total_tools,
                    "total_tests": self.report.total_tests,
                    "total_passed": self.report.total_passed,
                    "total_failed": self.report.total_failed,
                    "overall_success_rate": self.report.overall_success_rate,
                    "duration_seconds": self.report.duration_seconds,
                },
                "tool_summaries": [
                    {
                        "tool": s.tool, "category": s.category, "total_tests": s.total_tests,
                        "passed": s.passed, "failed": s.failed, "timeout": s.timeout,
                        "error": s.error, "success_rate": s.success_rate,
                        "avg_execution_time_ms": s.avg_execution_time_ms,
                    }
                    for s in self.report.tool_summaries
                ],
                "results": [r.to_dict() for r in self.results]
            }, f, indent=2, default=str)
        
        report_path = output_dir / f"test_report_{ts}.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("="*70 + "\n")
            f.write("GMAIL TOOLS TEST REPORT (REAL DATA)\n".center(70) + "\n")
            f.write("="*70 + "\n\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Duration: {self.report.duration_seconds:.1f} seconds\n\n")
            f.write("OVERALL STATISTICS\n")
            f.write("-" * 40 + "\n")
            f.write(f"Tools Tested:      {self.report.total_tools}\n")
            f.write(f"Total Tests:       {self.report.total_tests}\n")
            f.write(f"Passed:            {self.report.total_passed}\n")
            f.write(f"Failed:            {self.report.total_failed}\n")
            f.write(f"Success Rate:      {self.report.overall_success_rate:.1f}%\n\n")
            
            f.write("PER-TOOL BREAKDOWN\n")
            f.write("-" * 40 + "\n")
            for s in sorted(self.report.tool_summaries, key=lambda x: x.tool):
                f.write(f"\n{s.tool} ({s.category})\n")
                f.write(f"  Tests:    {s.total_tests}\n")
                f.write(f"  Passed:   {s.passed}\n")
                f.write(f"  Failed:   {s.failed}\n")
                f.write(f"  Success:  {s.success_rate:.1f}%\n")
        
        return json_path, report_path


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Gmail Tools Test Suite with Real Data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_gmail_tools_real_data.py              # Test all tools
  python test_gmail_tools_real_data.py --tool send_email    # Test specific tool
  python test_gmail_tools_real_data.py --category core      # Test category
  python test_gmail_tools_real_data.py --random 3   # 3 random queries per tool
        """
    )
    
    parser.add_argument("--tool", type=str, help="Test only this specific tool")
    parser.add_argument("--category", type=str,
                       choices=[c.name.lower() for c in ToolCategory] + [c.value.lower() for c in ToolCategory],
                       help="Test only tools in this category")
    parser.add_argument("--random", type=int, metavar="N", help="Randomly select N queries per tool")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per test in seconds")
    parser.add_argument("--max-emails", type=int, default=20, help="Max emails to fetch for real data")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for output files")
    
    args = parser.parse_args()
    
    tester = GmailToolTester(
        verbose=args.verbose,
        timeout=args.timeout,
        max_emails=args.max_emails
    )
    
    try:
        report = tester.run_all_tests(
            specific_tool=args.tool,
            specific_category=args.category,
            random_count=args.random
        )
        
        tester.print_summary()
        
        json_path, report_path = tester.save_results(args.output_dir)
        
        print(f"\n{Colors.BOLD}{Colors.INFO}Results saved:{Colors.RESET}")
        print(f"  JSON:   {json_path}")
        print(f"  Report: {report_path}")
        
        sys.exit(0 if report.overall_success_rate >= 80 else 1)
        
    except KeyboardInterrupt:
        print(f"\n\n{Colors.WARNING}Test run interrupted by user.{Colors.RESET}")
        sys.exit(130)
    except Exception as e:
        print(f"\n{Colors.ERROR}Fatal error: {e}{Colors.RESET}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
