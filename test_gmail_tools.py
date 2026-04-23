#!/usr/bin/env python3
"""
Gmail Tools Comprehensive Test Suite
====================================

Tests ALL Gmail tools through the orchestrator with realistic human-like queries.

Usage:
    python test_gmail_tools.py [--tool TOOL_NAME] [--category CATEGORY] [--random] [--verbose]

Examples:
    python test_gmail_tools.py                      # Test all tools
    python test_gmail_tools.py --tool send_email    # Test only send_email
    python test_gmail_tools.py --category core      # Test Core Email category
    python test_gmail_tools.py --random --verbose   # Random selection, verbose output

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
from typing import Optional, List, Dict, Any, Callable
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
    list_labels as get_labels,
    gmail_call,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Real Data Fetcher - Fetches actual Gmail data for testing
# ═══════════════════════════════════════════════════════════════════════════════

class GmailDataFetcher:
    """Fetches real data from Gmail account for use in test queries."""
    
    def __init__(self, max_emails: int = 20):
        self.max_emails = max_emails
        self._data: Optional[Dict[str, Any]] = None
        self._initialized = False
    
    def _ensure_auth(self) -> bool:
        """Ensure Gmail is authenticated. Returns False if not authenticated."""
        try:
            authenticate_gmail()
            return True
        except PermissionError:
            return False
    
    def fetch(self) -> Dict[str, Any]:
        """Fetch real data from Gmail. Returns dict with all available data."""
        if self._data is not None:
            return self._data
        
        if not self._ensure_auth():
            print(f"{Colors.WARNING}⚠ Gmail not authenticated. Run 'python auth.py' first.{Colors.RESET}")
            print(f"{Colors.DIM}   Falling back to dummy data for testing.{Colors.RESET}\n")
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
            "has_attachments": [],
            "dates": [],
        }
        
        try:
            # Fetch recent emails
            emails = get_emails(limit=self.max_emails)
            for email in emails:
                data["email_ids"].append(email.get("id"))
                data["thread_ids"].append(email.get("thread_id"))
                data["subjects"].append(email.get("subject", "No Subject"))
                data["dates"].append(email.get("date", ""))
                
                # Extract sender
                from_header = email.get("from", "")
                if "<" in from_header:
                    sender = from_header.split("<")[1].split(">")[0]
                else:
                    sender = from_header
                if sender and sender not in data["senders"]:
                    data["senders"].append(sender)
                
                # Check for attachments
                labels = email.get("labels", [])
                if "attachments" in email or any("attachment" in str(l).lower() for l in labels):
                    data["has_attachments"].append(email.get("id"))
            
            # Fetch unread emails
            try:
                unread = get_unread_emails()
                data["unread_ids"] = [e.get("id") for e in unread[:5]]
            except Exception:
                pass
            
            # Fetch starred emails
            try:
                starred = get_starred_emails()
                data["starred_ids"] = [e.get("id") for e in starred[:5]]
            except Exception:
                pass
            
            # Fetch labels
            try:
                service = authenticate_gmail()
                labels_result = gmail_call(
                    lambda: service.users().labels().list(userId='me').execute()
                )
                all_labels = labels_result.get('labels', [])
                # Filter out system labels for user-created ones
                user_labels = [l['name'] for l in all_labels 
                              if not l['id'].startswith('CATEGORY_') 
                              and l['name'] not in ('INBOX', 'SENT', 'TRASH', 'DRAFT', 'SPAM', 'UNREAD', 'STARRED', 'IMPORTANT')]
                data["labels"] = user_labels[:10] or ['Work', 'Personal', 'Important']
            except Exception as e:
                data["labels"] = ['Work', 'Personal', 'Important', 'Archive', 'Finance']
            
            self._data = data
            self._print_data_summary(data)
            return data
            
        except Exception as e:
            print(f"{Colors.ERROR}✗ Error fetching Gmail data: {e}{Colors.RESET}")
            print(f"{Colors.DIM}   Using fallback data.{Colors.RESET}")
            return self._get_fallback_data()
    
    def _get_fallback_data(self) -> Dict[str, Any]:
        """Return fallback dummy data when Gmail is not accessible."""
        return {
            "email_ids": ["dummy_msg_001", "dummy_msg_002", "dummy_msg_003"],
            "thread_ids": ["dummy_thread_001", "dummy_thread_002"],
            "senders": ["test@example.com", "demo@gmail.com"],
            "subjects": ["Test Email", "Demo Subject"],
            "labels": ["Work", "Personal", "Important"],
            "unread_ids": ["dummy_msg_001"],
            "starred_ids": ["dummy_msg_002"],
            "has_attachments": ["dummy_msg_003"],
            "dates": ["Mon, 15 Jan 2024 10:00:00 GMT"],
        }
    
    def _print_data_summary(self, data: Dict[str, Any]) -> None:
        """Print summary of fetched data."""
        print(f"  {Colors.SUCCESS}✓ Found:{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['email_ids'])} emails{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['thread_ids'])} threads{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['senders'])} unique senders{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['labels'])} labels{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['unread_ids'])} unread{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['starred_ids'])} starred{Colors.RESET}")
    
    def get_email_id(self, index: int = 0) -> str:
        """Get a real email ID, falling back to dummy if needed."""
        ids = self._data.get("email_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"msg_{index:03d}"
    
    def get_thread_id(self, index: int = 0) -> str:
        """Get a real thread ID."""
        ids = self._data.get("thread_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"thread_{index:03d}"
    
    def get_sender(self, index: int = 0) -> str:
        """Get a real sender email address."""
        senders = self._data.get("senders", []) if self._data else []
        if senders and index < len(senders):
            return senders[index]
        return "example@domain.com"
    
    def get_label(self, index: int = 0) -> str:
        """Get a real label name."""
        labels = self._data.get("labels", []) if self._data else []
        if labels and index < len(labels):
            return labels[index]
        return "Important"


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration & Constants
# ═══════════════════════════════════════════════════════════════════════════════

class Colors:
    """ANSI color codes for terminal output."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    
    # Status colors
    SUCCESS = "\033[92m"   # Green
    WARNING = "\033[93m"   # Yellow
    ERROR = "\033[91m"     # Red
    INFO = "\033[94m"       # Blue
    
    # Category colors
    CORE = "\033[96m"       # Cyan
    SEND = "\033[95m"       # Magenta
    LABELS = "\033[94m"     # Blue
    ATTACHMENTS = "\033[93m" # Yellow
    AI = "\033[92m"         # Green
    MISC = "\033[90m"       # Gray


class ToolCategory(Enum):
    """Gmail tool categories."""
    CORE = "Core Email Operations"
    SEND = "Sending & Drafting"
    LABELS = "Labels & Organization"
    ATTACHMENTS = "Attachments"
    SCHEDULING = "Scheduling & Reminders"
    SECURITY = "Security & Validation"
    ANALYTICS = "Analytics"
    AI = "AI-Powered Tools"


# Tool → Category mapping
TOOL_CATEGORIES = {
    # Core Email
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
    
    # Sending & Drafting
    "send_email": ToolCategory.SEND,
    "send_email_with_attachment": ToolCategory.SEND,
    "draft_email": ToolCategory.SEND,
    "send_draft": ToolCategory.SEND,
    "update_draft": ToolCategory.SEND,
    "delete_draft": ToolCategory.SEND,
    "reply_email": ToolCategory.SEND,
    "reply_all": ToolCategory.SEND,
    "forward_email": ToolCategory.SEND,
    
    # Labels & Organization
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
    
    # Attachments
    "get_attachments": ToolCategory.ATTACHMENTS,
    "download_attachment": ToolCategory.ATTACHMENTS,
    "save_attachment_to_disk": ToolCategory.ATTACHMENTS,
    
    # Scheduling
    "schedule_email": ToolCategory.SCHEDULING,
    "set_email_reminder": ToolCategory.SCHEDULING,
    
    # Security
    "confirm_action": ToolCategory.SECURITY,
    "validate_email_address": ToolCategory.SECURITY,
    "sanitize_email_content": ToolCategory.SECURITY,
    "log_email_action": ToolCategory.SECURITY,
    "audit_email_history": ToolCategory.SECURITY,
    
    # Analytics
    "count_emails_by_sender": ToolCategory.ANALYTICS,
    "email_activity_summary": ToolCategory.ANALYTICS,
    "most_frequent_contacts": ToolCategory.ANALYTICS,
    
    # AI Tools
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


# Category → Color mapping
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
# Dynamic Query Generator - Builds queries using real Gmail data
# ═══════════════════════════════════════════════════════════════════════════════

class QueryGenerator:
    """Generates realistic test queries using real Gmail data."""
    
    def __init__(self, data_fetcher: GmailDataFetcher):
        self.data = data_fetcher
    
    def _get_ids(self, count: int = 3) -> List[str]:
        """Get real email IDs from fetched data."""
        return [self.data.get_email_id(i) for i in range(min(count, 10))]
    
    def _get_thread_ids(self, count: int = 3) -> List[str]:
        """Get real thread IDs."""
        return [self.data.get_thread_id(i) for i in range(min(count, 10))]
    
    def _get_senders(self, count: int = 3) -> List[str]:
        """Get real sender email addresses."""
        return [self.data.get_sender(i) for i in range(min(count, 10))]
    
    def _get_labels(self, count: int = 3) -> List[str]:
        """Get real label names."""
        return [self.data.get_label(i) for i in range(min(count, 10))]
    
    def generate(self, tool: str) -> List[str]:
        """Generate 8-10 realistic queries for the specified tool."""
        method = getattr(self, f"_gen_{tool}", self._gen_default)
        return method()
    
    # ───────────────────────────────────────────────────────────────────────────
    # Core Email Operations
    # ───────────────────────────────────────────────────────────────────────────
    def _gen_authenticate_gmail(self) -> List[str]:
        return [
            "Connect to my Gmail account",
            "Authenticate with Gmail",
            "Sign in to Gmail",
            "Verify my Gmail connection",
            "Check Gmail authentication status",
            "Link my Gmail",
            "Authorize Gmail access",
            "Set up Gmail connection",
            "Initialize Gmail service",
            "Verify Gmail credentials",
        ]
    
    def _gen_get_emails(self) -> List[str]:
        return [
            "Show my recent emails",
            "Read my inbox",
            "Display my emails",
            "What emails do I have?",
            "Check my messages",
            "List my emails",
            "Get my inbox",
            "Show me my mail",
            "Fetch my emails",
            "View my inbox",
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
            "Show my unread emails",
            "Check unread messages",
            "What emails haven't I read?",
            "List unread mail",
            "Display unread emails",
            "Get my unread messages",
            "Show unread inbox items",
            "Fetch unread emails",
            "Any new emails I haven't seen?",
            "Check my unread",
        ]
    
    def _gen_get_starred_emails(self) -> List[str]:
        return [
            "Show my starred emails",
            "Check important messages",
            "Display starred items",
            "Get my flagged emails",
            "Show important emails",
            "List starred messages",
            "What did I star?",
            "Fetch starred emails",
            "View my starred mail",
            "Show my favorites",
        ]
    
    def _gen_search_emails(self) -> List[str]:
        # Use real subjects from fetched emails for search terms
        subjects = self.data._data.get("subjects", []) if self.data._data else []
        search_terms = subjects[:3] if subjects else ["invoice", "meeting", "update"]
        
        return [
            f"Search emails about {search_terms[0] if search_terms else 'project'}",
            f"Find messages containing '{search_terms[1] if len(search_terms) > 1 else 'report'}'",
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
            "Show mail from December 2024",
            "Get emails between March 15 and April 15",
            "Find messages from Q1 2024",
            "Show emails from 2024/06/01 to 2024/06/30",
            "Get mail between last Monday and Friday",
            "Find emails from the past month",
            "Show messages from date range 2024-01-01 to 2024-12-31",
        ]
    
    def _gen_get_email_thread(self) -> List[str]:
        thread_ids = self._get_thread_ids(5)
        return [
            f"Show the conversation thread {thread_ids[0]}",
            f"Get email thread with ID {thread_ids[1] if len(thread_ids) > 1 else thread_ids[0]}",
            f"View thread {thread_ids[2] if len(thread_ids) > 2 else thread_ids[0]}",
            f"Display conversation {thread_ids[0]}",
            f"Show thread for message {thread_ids[1] if len(thread_ids) > 1 else thread_ids[0]}",
            f"Get thread ID {thread_ids[2] if len(thread_ids) > 2 else thread_ids[0]}",
            f"View conversation thread {thread_ids[0]}",
            f"Show thread {thread_ids[1] if len(thread_ids) > 1 else thread_ids[0]}",
            f"Get email thread {thread_ids[2] if len(thread_ids) > 2 else thread_ids[0]}",
            f"Display thread {thread_ids[0]}",
        ]
    
    # ───────────────────────────────────────────────────────────────────────────
    # Sending & Drafting
    # ───────────────────────────────────────────────────────────────────────────
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
        # Draft IDs are typically different from message IDs
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
    
    # ───────────────────────────────────────────────────────────────────────────
    # Labels & Organization
    # ───────────────────────────────────────────────────────────────────────────
    def _gen_list_labels(self) -> List[str]:
        return [
            "Show all my Gmail labels",
            "List my email labels",
            "What labels do I have?",
            "Display my Gmail tags",
            "Get all my labels",
            "Show my label list",
            "List available labels",
            "What labels are in my Gmail?",
            "Show me all labels",
            "Display available Gmail labels",
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
            "Create a new label called 'Projects'",
            "Make label 'Urgent' in Gmail",
            "Add label 'Work-Personal'",
            "Create label named 'Finance-2024'",
            "Make new label 'Client-A'",
            "Create 'Receipts-2024' label",
            "Add label 'Travel-Plans'",
            "Create label 'Important-Archive'",
            "Make label 'Newsletter'",
            "Create new label 'Bills-To-Pay'",
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
    
    # ───────────────────────────────────────────────────────────────────────────
    # Attachments
    # ───────────────────────────────────────────────────────────────────────────
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
    
    # ───────────────────────────────────────────────────────────────────────────
    # Scheduling & Reminders
    # ───────────────────────────────────────────────────────────────────────────
    def _gen_schedule_email(self) -> List[str]:
        senders = self._get_senders(3)
        return [
            f"Schedule email to {senders[0]} for tomorrow at 9am",
            f"Send message to {senders[1] if len(senders) > 1 else senders[0]} on Monday morning",
            f"Schedule email to team@company.com for next week",
            f"Queue email to {senders[2] if len(senders) > 2 else senders[0]} for Friday afternoon",
            f"Schedule message to {senders[0]} for 2024-12-25",
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
    
    # ───────────────────────────────────────────────────────────────────────────
    # Security & Validation
    # ───────────────────────────────────────────────────────────────────────────
    def _gen_confirm_action(self) -> List[str]:
        return [
            "Confirm delete action on email",
            "Approve sending this message",
            "Confirm trash operation",
            "Approve archive action",
            "Confirm email deletion",
            "Approve label change",
            "Confirm draft deletion",
            "Approve forward action",
            "Confirm reply to all",
            "Approve bulk delete",
        ]
    
    def _gen_validate_email_address(self) -> List[str]:
        senders = self._get_senders(10)
        # Add some test emails
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
            "Clean email text for sending",
            "Sanitize content for safe display",
            "Clean this message body",
            "Sanitize email content with special characters",
            "Clean text for email composition",
            "Sanitize body content",
            "Clean email message text",
            "Sanitize content before sending",
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
            "Show my email audit history",
            "Get audit log of email actions",
            "View email action history",
            "Display audit trail for emails",
            "Show logged email operations",
            "Get history of email actions",
            "View email audit records",
            "Display email action log",
            "Show email audit trail",
            "Get audit history of messages",
        ]
    
    # ───────────────────────────────────────────────────────────────────────────
    # Analytics
    # ───────────────────────────────────────────────────────────────────────────
    def _gen_count_emails_by_sender(self) -> List[str]:
        return [
            "Count emails from each sender",
            "How many emails per contact?",
            "Show email count by sender",
            "Get statistics on email senders",
            "Count messages from each person",
            "Email distribution by sender",
            "How many from each email address?",
            "Sender email count statistics",
            "Count my emails per contact",
            "Email frequency by sender",
        ]
    
    def _gen_email_activity_summary(self) -> List[str]:
        return [
            "Show my email activity summary",
            "Get email statistics overview",
            "Display email usage summary",
            "What are my email activity stats?",
            "Show email analytics summary",
            "Get overview of email activity",
            "Display email statistics",
            "Show email usage report",
            "Get email activity overview",
            "Display email summary stats",
        ]
    
    def _gen_most_frequent_contacts(self) -> List[str]:
        return [
            "Who are my most frequent contacts?",
            "Show top email contacts",
            "Who emails me the most?",
            "List my most active contacts",
            "Show frequent email senders",
            "Get my top email contacts",
            "Who are my main contacts?",
            "Show most common email senders",
            "List frequent email partners",
            "Get top contact list",
        ]
    
    # ───────────────────────────────────────────────────────────────────────────
    # AI-Powered Tools
    # ───────────────────────────────────────────────────────────────────────────
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
            "Summarize my recent emails",
            "Give me TL;DR of my inbox",
            "Summarize last 10 messages",
            "Brief summary of unread emails",
            "Summarize today's emails",
            "Quick summary of my mail",
            "Summarize this week's messages",
            "Give overview of my emails",
            "Summarize important emails",
            "Brief digest of my inbox",
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
            "Auto-label my unread emails",
            "Suggest labels for recent messages",
            "Automatically categorize my inbox",
            "Auto-label emails from last week",
            "Suggest tags for unread mail",
            "Auto-categorize my messages",
            "Label my recent emails automatically",
            "Suggest labels for inbox items",
            "Auto-tag unread emails",
            "Automatically label my mail",
        ]
    
    def _gen_auto_archive_promotions(self) -> List[str]:
        return [
            "Auto-archive promotional emails",
            "Automatically archive marketing messages",
            "Archive promo emails automatically",
            "Auto-archive newsletter emails",
            "Automatically clean up promotions",
            "Auto-archive spam-like emails",
            "Archive marketing mail automatically",
            "Auto-archive bulk emails",
            "Automatically archive ads",
            "Auto-archive commercial emails",
        ]
    
    def _gen_auto_reply_rules(self) -> List[str]:
        return [
            "Suggest auto-reply rules for my inbox",
            "Create auto-reply suggestions",
            "What auto-replies should I set up?",
            "Suggest automatic response rules",
            "Generate auto-reply recommendations",
            "Create rules for auto-responses",
            "Suggest vacation auto-reply setup",
            "What auto-reply rules would help?",
            "Generate auto-response suggestions",
            "Suggest inbox auto-reply configuration",
        ]
    
    def _gen_default(self) -> List[str]:
        """Default generator for tools without specific implementation."""
        return [
            f"Execute {self.__class__.__name__}",
            f"Run {self.__class__.__name__} tool",
            f"Test {self.__class__.__name__} functionality",
        ]


# Query generator instance (initialized with real data)
_query_generator: Optional[QueryGenerator] = None

def get_query_generator(data_fetcher: GmailDataFetcher) -> QueryGenerator:
    """Get or create the global query generator."""
    global _query_generator
    if _query_generator is None:
        _query_generator = QueryGenerator(data_fetcher)
    return _query_generator


# ═══════════════════════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════════════════════
    ],
    "get_starred_emails": [
        "Show my starred emails",
        "Check important messages",
        "Display starred items",
        "Get my flagged emails",
        "Show important emails",
        "List starred messages",
        "What did I star?",
        "Fetch starred emails",
        "View my starred mail",
        "Show my favorites",
    ],
    "search_emails": [
        "Search emails about invoice",
        "Find messages containing 'project update'",
        "Search for emails with subject 'meeting'",
        "Look for emails about budget",
        "Find all emails regarding the conference",
        "Search my mail for 'contract'",
        "Find emails with 'urgent' in them",
        "Search for messages about the deadline",
        "Look for emails from last month about sales",
        "Find emails containing 'Q4 report'",
    ],
    "get_emails_by_sender": [
        "Show emails from john@company.com",
        "Get messages from sarah@gmail.com",
        "Find emails sent by boss@work.com",
        "Show mail from mom@family.net",
        "Get all emails from support@service.io",
        "Find messages from marketing@company.com",
        "Show emails from hr@company.com",
        "Get mail sent by admin@system.com",
        "Find emails from alice@partner.com",
        "Show messages from billing@company.com",
    ],
    "get_emails_by_label": [
        "Show emails labeled 'Work'",
        "Get messages with label 'Personal'",
        "Find emails tagged 'Important'",
        "Show mail labeled 'Projects'",
        "Get emails with 'Finance' label",
        "Find messages tagged 'Urgent'",
        "Show emails in label 'Archive'",
        "Get mail with 'Receipts' label",
        "Find emails labeled 'Travel'",
        "Show messages tagged 'Invoices'",
    ],
    "get_emails_by_date_range": [
        "Show emails from January 1 to January 31",
        "Get messages between 2024/01/01 and 2024/01/31",
        "Find emails from last week",
        "Show mail from December 2024",
        "Get emails between March 15 and April 15",
        "Find messages from Q1 2024",
        "Show emails from 2024/06/01 to 2024/06/30",
        "Get mail between last Monday and Friday",
        "Find emails from the past month",
        "Show messages from date range 2024-01-01 to 2024-12-31",
    ],
    "get_email_thread": [
        "Show the conversation thread 1234567890abcdef",
        "Get email thread with ID abc123xyz789",
        "View thread xyz789def456abc",
        "Display conversation 1a2b3c4d5e6f7g8h",
        "Show thread for message abcdef123456",
        "Get thread ID ending in 789abc123",
        "View conversation thread abc123",
        "Show thread 1234567890abcdef",
        "Get email thread xyz789",
        "Display thread 1a2b3c4d5e6f7g8h",
    ],
    
    # ───────────────────────────────────────────────────────────────────────────
    # Sending & Drafting
    # ───────────────────────────────────────────────────────────────────────────
    "send_email": [
        "Send an email to john@example.com saying I'll be late",
        "Mail my manager about the meeting delay",
        "Write and send a quick update to team@company.com",
        "Send message to sarah@gmail.com: Thanks for your help!",
        "Email boss@work.com about the project status",
        "Send a note to support@service.io requesting assistance",
        "Write email to hr@company.com about vacation days",
        "Send quick message to alice@partner.com confirming our call",
        "Email admin@system.com asking for access",
        "Send update to marketing@company.com about the campaign",
    ],
    "send_email_with_attachment": [
        "Send email with attachment to john@example.com",
        "Mail document to boss@work.com with file attached",
        "Send report.pdf to team@company.com",
        "Email sarah@gmail.com with the spreadsheet attached",
        "Send presentation to alice@partner.com with attachment",
        "Mail invoice.pdf to billing@company.com",
        "Send contract with attachment to legal@company.com",
        "Email mom the photos as attachments",
        "Send file to support@service.io with my request",
        "Mail report with document to manager@work.com",
    ],
    "draft_email": [
        "Draft an email to john@example.com about the proposal",
        "Create a draft message for team@company.com",
        "Write draft email to sarah@gmail.com",
        "Draft message to boss@work.com about the delay",
        "Compose draft for alice@partner.com",
        "Create email draft to hr@company.com",
        "Draft a note to support@service.io",
        "Write draft message to marketing@company.com",
        "Draft email for admin@system.com",
        "Create draft for billing@company.com",
    ],
    "send_draft": [
        "Send draft ID draft_abc123xyz789",
        "Send my saved draft with ID r1234567890abc",
        "Dispatch draft message 1a2b3c4d5e6f7g8h",
        "Send the draft xyz789def456abc",
        "Mail out draft ID ending in 789abc123",
        "Send draft number abc123def456",
        "Dispatch saved draft 1234567890abcdef",
        "Send draft xyz789",
        "Mail draft 1a2b3c4d5e6f7g8h",
        "Send the draft with ID abc123",
    ],
    "update_draft": [
        "Update draft abc123 with new content",
        "Edit draft ID xyz789 to add more details",
        "Modify draft 1a2b3c4d with changes",
        "Update draft ending in 789abc with new text",
        "Edit draft message abcdef123456",
        "Revise draft ID xyz789def456abc",
        "Update the content of draft 1234567890",
        "Modify draft abc123xyz789",
        "Edit draft 1a2b3c4d5e6f7g8h",
        "Update draft xyz789 with corrections",
    ],
    "delete_draft": [
        "Delete draft ID abc123xyz789",
        "Remove draft xyz789def456abc",
        "Discard draft 1a2b3c4d5e6f7g8h",
        "Delete draft ending in 789abc123",
        "Remove draft message abcdef123456",
        "Trash draft ID xyz789",
        "Delete draft 1234567890abcdef",
        "Remove draft abc123def456",
        "Discard draft xyz789abc123",
        "Delete the draft with ID 1a2b3c4d",
    ],
    "reply_email": [
        "Reply to email abc123xyz789 saying thanks",
        "Respond to message 1a2b3c4d5e6f7g8h",
        "Reply to xyz789def456abc with confirmation",
        "Send reply to email ending in 789abc123",
        "Respond to abcdef1234567890",
        "Reply to message ID 1234567890abcdef",
        "Answer email xyz789",
        "Reply to abc123def456 with details",
        "Respond to email 1a2b3c4d5e6f7g8h",
        "Reply to xyz789abc123 saying approved",
    ],
    "reply_all": [
        "Reply all to email abc123xyz789",
        "Respond to everyone on message 1a2b3c4d",
        "Reply all for thread xyz789def456abc",
        "Send reply all to email ending in 789abc",
        "Respond to all recipients of abcdef123456",
        "Reply all to message ID 1234567890",
        "Answer all on email xyz789",
        "Reply all to abc123def456",
        "Respond to everyone on email 1a2b3c4d",
        "Reply all for xyz789abc123",
    ],
    "forward_email": [
        "Forward email abc123xyz789 to sarah@example.com",
        "Send message 1a2b3c4d5e6f7g8h to john@work.com",
        "Forward xyz789def456abc to team@company.com",
        "Forward email ending in 789abc123 to alice@partner.com",
        "Send abcdef1234567890 to boss@work.com",
        "Forward message ID 1234567890abcdef to hr@company.com",
        "Forward xyz789 to support@service.io",
        "Send abc123def456 to marketing@company.com",
        "Forward email 1a2b3c4d5e6f7g8h to admin@system.com",
        "Forward xyz789abc123 to billing@company.com",
    ],
    
    # ───────────────────────────────────────────────────────────────────────────
    # Labels & Organization
    # ───────────────────────────────────────────────────────────────────────────
    "list_labels": [
        "Show all my Gmail labels",
        "List my email labels",
        "What labels do I have?",
        "Display my Gmail tags",
        "Get all my labels",
        "Show my label list",
        "List available labels",
        "What labels are in my Gmail?",
        "Show me all labels",
        "Display available Gmail labels",
    ],
    "add_label": [
        "Add label 'Work' to email abc123xyz789",
        "Tag message 1a2b3c4d with 'Important'",
        "Apply label 'Urgent' to xyz789def456abc",
        "Add label 'Personal' to email ending in 789abc",
        "Tag abcdef123456 with 'Finance'",
        "Apply 'Projects' label to message ID 1234567890",
        "Add 'Archive' label to xyz789",
        "Tag abc123def456 with 'Receipts'",
        "Apply label 'Travel' to email 1a2b3c4d",
        "Add 'Invoices' label to xyz789abc123",
    ],
    "remove_label": [
        "Remove label 'Work' from email abc123xyz789",
        "Untag message 1a2b3c4d from 'Important'",
        "Remove 'Urgent' label from xyz789def456abc",
        "Delete label 'Personal' from email ending in 789abc",
        "Untag abcdef123456 from 'Finance'",
        "Remove 'Projects' label from message ID 1234567890",
        "Delete 'Archive' label from xyz789",
        "Untag abc123def456 from 'Receipts'",
        "Remove 'Travel' label from email 1a2b3c4d",
        "Delete 'Invoices' label from xyz789abc123",
    ],
    "create_label": [
        "Create a new label called 'Projects'",
        "Make label 'Urgent' in Gmail",
        "Add label 'Work-Personal'",
        "Create label named 'Finance-2024'",
        "Make new label 'Client-A'",
        "Create 'Receipts-2024' label",
        "Add label 'Travel-Plans'",
        "Create label 'Important-Archive'",
        "Make label 'Newsletter'",
        "Create new label 'Bills-To-Pay'",
    ],
    "mark_as_read": [
        "Mark email abc123xyz789 as read",
        "Set message 1a2b3c4d to read",
        "Mark xyz789def456abc as read",
        "Set email ending in 789abc123 as read",
        "Mark abcdef1234567890 as read",
        "Set message ID 1234567890 as read",
        "Mark xyz789 as read",
        "Set abc123def456 to read",
        "Mark email 1a2b3c4d5e6f7g8h as read",
        "Set xyz789abc123 to read",
    ],
    "mark_as_unread": [
        "Mark email abc123xyz789 as unread",
        "Set message 1a2b3c4d to unread",
        "Mark xyz789def456abc as unread",
        "Set email ending in 789abc123 as unread",
        "Mark abcdef1234567890 as unread",
        "Set message ID 1234567890 as unread",
        "Mark xyz789 as unread",
        "Set abc123def456 to unread",
        "Mark email 1a2b3c4d5e6f7g8h as unread",
        "Set xyz789abc123 to unread",
    ],
    "star_email": [
        "Star email abc123xyz789",
        "Add star to message 1a2b3c4d",
        "Star xyz789def456abc",
        "Flag email ending in 789abc123",
        "Star abcdef1234567890",
        "Add star to message ID 1234567890",
        "Star xyz789",
        "Flag abc123def456",
        "Star email 1a2b3c4d5e6f7g8h",
        "Add star to xyz789abc123",
    ],
    "unstar_email": [
        "Unstar email abc123xyz789",
        "Remove star from message 1a2b3c4d",
        "Unstar xyz789def456abc",
        "Remove flag from email ending in 789abc123",
        "Unstar abcdef1234567890",
        "Remove star from message ID 1234567890",
        "Unstar xyz789",
        "Unflag abc123def456",
        "Unstar email 1a2b3c4d5e6f7g8h",
        "Remove star from xyz789abc123",
    ],
    "archive_email": [
        "Archive email abc123xyz789",
        "Archive message 1a2b3c4d5e6f7g8h",
        "Move xyz789def456abc to archive",
        "Archive email ending in 789abc123",
        "Archive abcdef1234567890",
        "Archive message ID 1234567890abcdef",
        "Archive xyz789",
        "Move abc123def456 to archive",
        "Archive email 1a2b3c4d5e6f7g8h",
        "Archive xyz789abc123",
    ],
    "unarchive_email": [
        "Unarchive email abc123xyz789",
        "Restore message 1a2b3c4d to inbox",
        "Unarchive xyz789def456abc",
        "Move email ending in 789abc123 to inbox",
        "Unarchive abcdef1234567890",
        "Restore message ID 1234567890 to inbox",
        "Unarchive xyz789",
        "Move abc123def456 to inbox",
        "Unarchive email 1a2b3c4d5e6f7g8h",
        "Restore xyz789abc123 to inbox",
    ],
    "move_to_folder": [
        "Move email abc123xyz789 to 'Work' folder",
        "Move message 1a2b3c4d to folder 'Personal'",
        "Put xyz789def456abc in 'Projects' folder",
        "Move email ending in 789abc123 to 'Archive'",
        "Move abcdef123456 to 'Finance' folder",
        "Put message ID 1234567890 in 'Important'",
        "Move xyz789 to 'Receipts' folder",
        "Put abc123def456 in 'Travel' folder",
        "Move email 1a2b3c4d5e6f7g8h to 'Newsletter'",
        "Put xyz789abc123 in 'Bills' folder",
    ],
    "trash_email": [
        "Trash email abc123xyz789",
        "Delete message 1a2b3c4d5e6f7g8h",
        "Move xyz789def456abc to trash",
        "Trash email ending in 789abc123",
        "Delete abcdef1234567890",
        "Trash message ID 1234567890abcdef",
        "Delete xyz789",
        "Move abc123def456 to trash",
        "Trash email 1a2b3c4d5e6f7g8h",
        "Delete xyz789abc123",
    ],
    "restore_email": [
        "Restore email abc123xyz789 from trash",
        "Recover message 1a2b3c4d from trash",
        "Restore xyz789def456abc",
        "Undelete email ending in 789abc123",
        "Restore abcdef1234567890 from trash",
        "Recover message ID 1234567890abcdef",
        "Restore xyz789",
        "Undelete abc123def456",
        "Restore email 1a2b3c4d5e6f7g8h",
        "Recover xyz789abc123 from trash",
    ],
    "delete_email": [
        "Permanently delete email abc123xyz789",
        "Erase message 1a2b3c4d5e6f7g8h forever",
        "Delete xyz789def456abc permanently",
        "Erase email ending in 789abc123",
        "Permanently remove abcdef1234567890",
        "Delete forever message ID 1234567890abcdef",
        "Erase xyz789 permanently",
        "Permanently delete abc123def456",
        "Delete forever email 1a2b3c4d5e6f7g8h",
        "Erase xyz789abc123 permanently",
    ],
    
    # ─────────────────═══════════════════════════════════════════════════════════
    # Attachments
    # ───────────────────────────────────────────────────────────────────────────
    "get_attachments": [
        "Show attachments on email abc123xyz789",
        "List attachments for message 1a2b3c4d",
        "What files are attached to xyz789def456abc?",
        "Get attachments from email ending in 789abc",
        "Show files on abcdef1234567890",
        "List attachments for message ID 1234567890",
        "What attachments does xyz789 have?",
        "Show attached files on abc123def456",
        "List attachments on email 1a2b3c4d5e6f7g8h",
        "Get files attached to xyz789abc123",
    ],
    "download_attachment": [
        "Download attachment from email abc123xyz789",
        "Get the file attached to message 1a2b3c4d",
        "Download files from xyz789def456abc",
        "Save attachments from email ending in 789abc",
        "Download files on abcdef1234567890",
        "Get attachment for message ID 1234567890",
        "Download the file from xyz789",
        "Save attachment from abc123def456",
        "Download files from email 1a2b3c4d5e6f7g8h",
        "Get attachments from xyz789abc123",
    ],
    "save_attachment_to_disk": [
        "Save attachment to disk from email abc123xyz789",
        "Download and save files from message 1a2b3c4d",
        "Save attached file from xyz789def456abc to my computer",
        "Download attachment to disk from email ending in 789abc",
        "Save files on abcdef1234567890 to downloads",
        "Download and save attachment for message ID 1234567890",
        "Save the file from xyz789 to my downloads",
        "Download attachment to disk from abc123def456",
        "Save attached file from email 1a2b3c4d5e6f7g8h",
        "Download and save from xyz789abc123",
    ],
    
    # ───────────────────────────────────────────────────────────────────────────
    # Scheduling & Reminders
    # ─────────────────═══════════════════════════════════════════════════════════
    "schedule_email": [
        "Schedule email to john@example.com for tomorrow at 9am",
        "Send message to sarah@gmail.com on Monday morning",
        "Schedule email to team@company.com for next week",
        "Queue email to boss@work.com for Friday afternoon",
        "Schedule message to alice@partner.com for 2024-12-25",
        "Send email to hr@company.com on January 1st",
        "Schedule message to support@service.io for 3pm today",
        "Queue email to marketing@company.com for next month",
        "Schedule email to admin@system.com for tomorrow evening",
        "Send message to billing@company.com on the first of next month",
    ],
    "set_email_reminder": [
        "Set reminder for email abc123xyz789 tomorrow",
        "Remind me about message 1a2b3c4d in 2 hours",
        "Create reminder for xyz789def456abc next week",
        "Set email reminder for abcdef1234567890 on Friday",
        "Remind me about message ID 1234567890abcdef in 3 days",
        "Add reminder for xyz789 at 5pm today",
        "Set reminder for abc123def456 tomorrow morning",
        "Remind me about email 1a2b3c4d5e6f7g8h next Monday",
        "Create reminder for xyz789abc123 in 1 week",
        "Set email reminder for abc123xyz789 at 10am",
    ],
    
    # ───────────────────────────────────────────────────────────────────────────
    # Security & Validation
    # ───────────────────────────────────────────────────────────────────────────
    "confirm_action": [
        "Confirm delete action on email",
        "Approve sending this message",
        "Confirm trash operation",
        "Approve archive action",
        "Confirm email deletion",
        "Approve label change",
        "Confirm draft deletion",
        "Approve forward action",
        "Confirm reply to all",
        "Approve bulk delete",
    ],
    "validate_email_address": [
        "Check if john@example.com is valid",
        "Validate email address sarah@gmail.com",
        "Is boss@work.com a proper email?",
        "Verify alice@partner.com format",
        "Check email validation for support@service.io",
        "Validate admin@system.com",
        "Is marketing@company.com valid?",
        "Check hr@company.com format",
        "Validate billing@company.com",
        "Is team@company.com a real email?",
    ],
    "sanitize_email_content": [
        "Clean this email content: <script>alert('xss')</script>",
        "Sanitize message body with HTML tags",
        "Clean email text for sending",
        "Sanitize content for safe display",
        "Clean this message body",
        "Sanitize email content with special characters",
        "Clean text for email composition",
        "Sanitize body content",
        "Clean email message text",
        "Sanitize content before sending",
    ],
    "log_email_action": [
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
    ],
    "audit_email_history": [
        "Show my email audit history",
        "Get audit log of email actions",
        "View email action history",
        "Display audit trail for emails",
        "Show logged email operations",
        "Get history of email actions",
        "View email audit records",
        "Display email action log",
        "Show email audit trail",
        "Get audit history of messages",
    ],
    
    # ───────────────────────────────────────────────────────────────────────────
    # Analytics
    # ───────────────────────────────────────────────────────────────────────────
    "count_emails_by_sender": [
        "Count emails from each sender",
        "How many emails per contact?",
        "Show email count by sender",
        "Get statistics on email senders",
        "Count messages from each person",
        "Email distribution by sender",
        "How many from each email address?",
        "Sender email count statistics",
        "Count my emails per contact",
        "Email frequency by sender",
    ],
    "email_activity_summary": [
        "Show my email activity summary",
        "Get email statistics overview",
        "Display email usage summary",
        "What are my email activity stats?",
        "Show email analytics summary",
        "Get overview of email activity",
        "Display email statistics",
        "Show email usage report",
        "Get email activity overview",
        "Display email summary stats",
    ],
    "most_frequent_contacts": [
        "Who are my most frequent contacts?",
        "Show top email contacts",
        "Who emails me the most?",
        "List my most active contacts",
        "Show frequent email senders",
        "Get my top email contacts",
        "Who are my main contacts?",
        "Show most common email senders",
        "List frequent email partners",
        "Get top contact list",
    ],
    
    # ───────────────────────────────────────────────────────────────────────────
    # AI-Powered Tools
    # ───────────────────────────────────────────────────────────────────────────
    "summarize_email": [
        "Summarize email abc123xyz789",
        "Give me a summary of message 1a2b3c4d",
        "TL;DR for xyz789def456abc",
        "Summarize email ending in 789abc123",
        "Brief summary of abcdef1234567890",
        "Summarize message ID 1234567890abcdef",
        "Get summary of xyz789",
        "Summarize abc123def456 for me",
        "Brief version of email 1a2b3c4d5e6f7g8h",
        "Summarize xyz789abc123",
    ],
    "summarize_emails": [
        "Summarize my recent emails",
        "Give me TL;DR of my inbox",
        "Summarize last 10 messages",
        "Brief summary of unread emails",
        "Summarize today's emails",
        "Quick summary of my mail",
        "Summarize this week's messages",
        "Give overview of my emails",
        "Summarize important emails",
        "Brief digest of my inbox",
    ],
    "classify_email": [
        "Classify email abc123xyz789",
        "What category is message 1a2b3c4d?",
        "Classify xyz789def456abc",
        "What type is email ending in 789abc?",
        "Classify abcdef1234567890",
        "Categorize message ID 1234567890",
        "What category is xyz789?",
        "Classify abc123def456",
        "Determine category of email 1a2b3c4d",
        "Classify xyz789abc123",
    ],
    "detect_urgency": [
        "Check urgency of email abc123xyz789",
        "Is message 1a2b3c4d urgent?",
        "How urgent is xyz789def456abc?",
        "Detect urgency for email ending in 789abc",
        "Check if abcdef1234567890 is urgent",
        "Is message ID 1234567890 time-sensitive?",
        "Check urgency of xyz789",
        "How critical is abc123def456?",
        "Detect urgency for email 1a2b3c4d",
        "Is xyz789abc123 urgent?",
    ],
    "detect_action_required": [
        "Does email abc123xyz789 need action?",
        "Check if message 1a2b3c4d requires response",
        "What action needed for xyz789def456abc?",
        "Detect action items in email ending in 789abc",
        "Check abcdef1234567890 for action required",
        "Does message ID 1234567890 need follow-up?",
        "What should I do about xyz789?",
        "Check abc123def456 for action items",
        "Detect required actions for email 1a2b3c4d",
        "Does xyz789abc123 need my attention?",
    ],
    "sentiment_analysis": [
        "Analyze sentiment of email abc123xyz789",
        "What tone does message 1a2b3c4d have?",
        "Check sentiment of xyz789def456abc",
        "Is email ending in 789abc positive or negative?",
        "Sentiment of abcdef1234567890",
        "Analyze tone of message ID 1234567890",
        "What sentiment is xyz789?",
        "Check abc123def456 sentiment",
        "Analyze email 1a2b3c4d5e6f7g8h tone",
        "Sentiment of xyz789abc123",
    ],
    "extract_tasks": [
        "Extract tasks from email abc123xyz789",
        "What to-dos are in message 1a2b3c4d?",
        "Get action items from xyz789def456abc",
        "Extract tasks from email ending in 789abc",
        "What tasks in abcdef1234567890?",
        "Extract to-dos from message ID 1234567890",
        "Get tasks from xyz789",
        "What action items in abc123def456?",
        "Extract tasks from email 1a2b3c4d",
        "Get to-do list from xyz789abc123",
    ],
    "extract_dates": [
        "Extract dates from email abc123xyz789",
        "What deadlines in message 1a2b3c4d?",
        "Find dates mentioned in xyz789def456abc",
        "Extract dates from email ending in 789abc",
        "What dates in abcdef1234567890?",
        "Find dates in message ID 1234567890",
        "Get dates from xyz789",
        "Extract deadlines from abc123def456",
        "What dates mentioned in email 1a2b3c4d?",
        "Find dates in xyz789abc123",
    ],
    "extract_contacts": [
        "Extract contacts from email abc123xyz789",
        "What contact info in message 1a2b3c4d?",
        "Find phone numbers in xyz789def456abc",
        "Extract contacts from email ending in 789abc",
        "What people mentioned in abcdef1234567890?",
        "Find contact details in message ID 1234567890",
        "Get contacts from xyz789",
        "Extract people from abc123def456",
        "What contacts in email 1a2b3c4d?",
        "Find names and emails in xyz789abc123",
    ],
    "extract_links": [
        "Extract links from email abc123xyz789",
        "What URLs in message 1a2b3c4d?",
        "Find links in xyz789def456abc",
        "Extract URLs from email ending in 789abc",
        "What web links in abcdef1234567890?",
        "Find all links in message ID 1234567890",
        "Get URLs from xyz789",
        "Extract links from abc123def456",
        "What links in email 1a2b3c4d?",
        "Find web addresses in xyz789abc123",
    ],
    "draft_reply": [
        "Draft a reply to email abc123xyz789",
        "Create response draft for message 1a2b3c4d",
        "Write reply draft for xyz789def456abc",
        "Draft response to email ending in 789abc",
        "Create reply for abcdef1234567890",
        "Write draft response for message ID 1234567890",
        "Draft reply to xyz789",
        "Create response draft for abc123def456",
        "Write reply for email 1a2b3c4d",
        "Draft response to xyz789abc123",
    ],
    "generate_followup": [
        "Generate follow-up for email abc123xyz789",
        "Create follow-up message for 1a2b3c4d",
        "Write follow-up for xyz789def456abc",
        "Generate follow-up to email ending in 789abc",
        "Create chase-up for abcdef1234567890",
        "Write follow-up for message ID 1234567890",
        "Generate follow-up to xyz789",
        "Create follow-up for abc123def456",
        "Write chase-up for email 1a2b3c4d",
        "Generate follow-up to xyz789abc123",
    ],
    "auto_reply": [
        "Generate auto-reply for email abc123xyz789",
        "Create auto-response for message 1a2b3c4d",
        "Write auto-reply for xyz789def456abc",
        "Generate auto-response to email ending in 789abc",
        "Create auto-reply for abcdef1234567890",
        "Write auto-response for message ID 1234567890",
        "Generate auto-reply to xyz789",
        "Create auto-response for abc123def456",
        "Write auto-reply for email 1a2b3c4d",
        "Generate auto-response to xyz789abc123",
    ],
    "rewrite_email": [
        "Rewrite email abc123xyz789 professionally",
        "Make message 1a2b3c4d more formal",
        "Rewrite xyz789def456abc casually",
        "Make email ending in 789abc friendlier",
        "Rewrite abcdef1234567890 concisely",
        "Make message ID 1234567890 more polite",
        "Rewrite xyz789 professionally",
        "Make abc123def456 more assertive",
        "Rewrite email 1a2b3c4d in simpler language",
        "Make xyz789abc123 more formal",
    ],
    "translate_email": [
        "Translate email abc123xyz789 to Spanish",
        "Convert message 1a2b3c4d to French",
        "Translate xyz789def456abc to German",
        "Convert email ending in 789abc to Japanese",
        "Translate abcdef1234567890 to Chinese",
        "Convert message ID 1234567890 to Italian",
        "Translate xyz789 to Portuguese",
        "Convert abc123def456 to Russian",
        "Translate email 1a2b3c4d to Arabic",
        "Convert xyz789abc123 to Hindi",
    ],
    "auto_label_emails": [
        "Auto-label my unread emails",
        "Suggest labels for recent messages",
        "Automatically categorize my inbox",
        "Auto-label emails from last week",
        "Suggest tags for unread mail",
        "Auto-categorize my messages",
        "Label my recent emails automatically",
        "Suggest labels for inbox items",
        "Auto-tag unread emails",
        "Automatically label my mail",
    ],
    "auto_archive_promotions": [
        "Auto-archive promotional emails",
        "Automatically archive marketing messages",
        "Archive promo emails automatically",
        "Auto-archive newsletter emails",
        "Automatically clean up promotions",
        "Auto-archive spam-like emails",
        "Archive marketing mail automatically",
        "Auto-archive bulk emails",
        "Automatically archive ads",
        "Auto-archive commercial emails",
    ],

@dataclass
class TestResult:
    """Result of a single test execution."""
    tool: str
    category: str
    query: str
    status: str  # "success", "failure", "timeout", "error"
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
    """Summary statistics for a single tool."""
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
    """Complete test run report."""
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
# Console Output Helpers
# ═══════════════════════════════════════════════════════════════════════════════

class Console:
    """Colored console output handler."""
    
    @staticmethod
    def print_header(text: str) -> None:
        print(f"\n{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{text.center(70)}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}\n")
    
    @staticmethod
    def print_section(text: str, category: ToolCategory) -> None:
        color = CATEGORY_COLORS.get(category, Colors.INFO)
        print(f"\n{color}{Colors.BOLD}{text}{Colors.RESET}")
        print(f"{color}{'─' * len(text)}{Colors.RESET}")
    
    @staticmethod
    def print_tool_header(tool: str, category: ToolCategory, index: int, total: int) -> None:
        color = CATEGORY_COLORS.get(category, Colors.INFO)
        progress = f"[{index}/{total}]"
        print(f"\n  {color}▶ {tool} {Colors.DIM}{progress}{Colors.RESET}")
    
    @staticmethod
    def print_query(query: str, status: str, execution_time: float) -> None:
        status_colors = {
            "success": Colors.SUCCESS,
            "failure": Colors.ERROR,
            "timeout": Colors.WARNING,
            "error": Colors.ERROR,
        }
        color = status_colors.get(status, Colors.INFO)
        status_icon = "✓" if status == "success" else "✗" if status in ("failure", "error") else "⏱"
        print(f"    {color}{status_icon} {Colors.DIM}{execution_time:.0f}ms{Colors.RESET} {query[:50]}...")
    
    @staticmethod
    def print_summary_line(label: str, value: str, color: str = Colors.RESET) -> None:
        print(f"  {Colors.BOLD}{label:.<30}{Colors.RESET} {color}{value}{Colors.RESET}")
    
    @staticmethod
    def print_error(msg: str) -> None:
        print(f"  {Colors.ERROR}✗ ERROR: {msg}{Colors.RESET}")
    
    @staticmethod
    def print_warning(msg: str) -> None:
        print(f"  {Colors.WARNING}⚠ {msg}{Colors.RESET}")
    
    @staticmethod
    def print_success(msg: str) -> None:
        print(f"  {Colors.SUCCESS}✓ {msg}{Colors.RESET}")


# ═══════════════════════════════════════════════════════════════════════════════
# Tool Execution Monitor
# ═══════════════════════════════════════════════════════════════════════════════

class ToolMonitor:
    """Monitors tool execution by wrapping the MCP server."""
    
    def __init__(self, mcp_server):
        self.mcp = mcp_server
        self.last_tool: Optional[str] = None
        self.last_args: Optional[Dict] = None
        self.last_result: Optional[Any] = None
    
    def execute_tool(self, name: str, args: Optional[Dict] = None) -> Any:
        """Execute tool and capture execution details."""
        self.last_tool = name
        self.last_args = args or {}
        self.last_result = self.mcp.execute_tool(name, args)
        return self.last_result
    
    def reset(self) -> None:
        """Reset captured state."""
        self.last_tool = None
        self.last_args = None
        self.last_result = None


# ═══════════════════════════════════════════════════════════════════════════════
# Test Runner
# ═══════════════════════════════════════════════════════════════════════════════

class GmailToolTester:
    """Main test runner for Gmail tools."""
    
    DEFAULT_TIMEOUT_SECONDS = 30
    MAX_RETRIES = 1
    
    def __init__(self, verbose: bool = False, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.verbose = verbose
        self.timeout = timeout
        self.console = Console()
        self.results: List[TestResult] = []
        self.report = TestReport(
            start_time=datetime.now().isoformat(),
            end_time=""
        )
        
        # Initialize MCP server and monitor
        self.mcp = build_server()
        self.monitor = ToolMonitor(self.mcp)
        
        # Track tool execution by patching monitor into orchestrator context
        self.state: Dict[str, Any] = {}
    
    def _detect_executed_tool(self, response: str) -> Optional[str]:
        """Detect which tool was executed based on response content."""
        # Look for tool result indicators in response
        import re
        
        # Pattern: "Tool result (tool_name):" or similar
        patterns = [
            r'Tool result \(([^)]+)\)',
            r'✅?\s*(\w+)\s*(?:sent|done|complete|created|moved|deleted|updated)',
            r'(\w+)\s+(?:successful|completed|executed)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, response, re.IGNORECASE)
            if match:
                return match.group(1)
        
        # Check for HTML formatted tool results
        if "Error:" in response:
            # Check if it's a specific tool error
            for tool_name in GMAIL_TOOLS.keys():
                if tool_name.lower() in response.lower():
                    return tool_name
        
        return None
    
    def _validate_result(self, result: TestResult) -> bool:
        """Basic validation of test result."""
        # Check if we got any response
        if not result.response or result.response.strip() == "":
            return False
        
        # Check for error indicators in response
        error_indicators = [
            "error:", "failed", "exception", "traceback",
            "not found", "invalid", "unauthorized", "permission denied"
        ]
        
        response_lower = result.response.lower()
        has_error = any(indicator in response_lower for indicator in error_indicators)
        
        # If status is success but response has error, it's a validation failure
        if result.status == "success" and has_error:
            # But some errors might be expected (e.g., auth errors without credentials)
            if "authentication" in response_lower or "not authenticated" in response_lower:
                return True  # Expected without proper auth
            return False
        
        # Check that some expected content is present
        if result.status == "success":
            # Response should have meaningful content
            if len(result.response) < 10:  # Too short
                return False
        
        return True
    
    @contextmanager
    def _timeout_context(self, seconds: int):
        """Context manager for timeout handling."""
        import signal
        
        def timeout_handler(signum, frame):
            raise TimeoutError(f"Test execution exceeded {seconds} seconds")
        
        # Set up timeout (Unix only; Windows would need different approach)
        try:
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(seconds)
            yield
        except AttributeError:
            # Windows doesn't have SIGALRM, just yield without timeout
            yield
        finally:
            try:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
            except (AttributeError, NameError):
                pass
    
    def run_single_test(self, tool: str, query: str) -> TestResult:
        """Execute a single test query through the orchestrator."""
        start_time = time.time()
        category = TOOL_CATEGORIES.get(tool, ToolCategory.CORE)
        
        # Reset state for fresh test
        self.state = {}
        self.monitor.reset()
        
        try:
            # Execute through orchestrator
            with self._timeout_context(self.timeout):
                response = run_agent(query, self.monitor, self.state, mode="gmail")
            
            execution_time = (time.time() - start_time) * 1000
            
            # Detect which tool was executed
            selected_tool = self.monitor.last_tool or self._detect_executed_tool(response)
            
            # Determine status
            if "error" in response.lower() or "failed" in response.lower():
                status = "failure"
            else:
                status = "success"
            
            result = TestResult(
                tool=tool,
                category=category.value,
                query=query,
                status=status,
                selected_tool=selected_tool,
                response=response[:500] if len(response) > 500 else response,  # Truncate for storage
                error=None,
                execution_time_ms=execution_time
            )
            
            result.validation_passed = self._validate_result(result)
            
        except TimeoutError as e:
            result = TestResult(
                tool=tool,
                category=category.value,
                query=query,
                status="timeout",
                error=str(e),
                execution_time_ms=self.timeout * 1000
            )
        except Exception as e:
            execution_time = (time.time() - start_time) * 1000
            result = TestResult(
                tool=tool,
                category=category.value,
                query=query,
                status="error",
                error=f"{type(e).__name__}: {str(e)}\n{traceback.format_exc()}",
                execution_time_ms=execution_time
            )
        
        return result
    
    def run_tool_tests(self, tool: str, queries: List[str], random_select: Optional[int] = None) -> List[TestResult]:
        """Run all tests for a specific tool."""
        category = TOOL_CATEGORIES.get(tool, ToolCategory.CORE)
        
        # Select queries
        if random_select and random_select < len(queries):
            selected_queries = random.sample(queries, random_select)
        else:
            selected_queries = queries
        
        results = []
        for i, query in enumerate(selected_queries, 1):
            if self.verbose:
                print(f"    Running query {i}/{len(selected_queries)}: {query[:60]}...")
            
            result = self.run_single_test(tool, query)
            results.append(result)
            
            # Print result
            self.console.print_query(query, result.status, result.execution_time_ms)
            
            if result.error and self.verbose:
                self.console.print_error(result.error[:100])
        
        return results
    
    def run_all_tests(
        self,
        specific_tool: Optional[str] = None,
        specific_category: Optional[str] = None,
        random_count: Optional[int] = None
    ) -> TestReport:
        """Run complete test suite."""
        self.console.print_header("GMAIL TOOLS COMPREHENSIVE TEST SUITE")
        
        # Determine which tools to test
        tools_to_test = []
        
        if specific_tool:
            if specific_tool in TEST_QUERIES:
                tools_to_test = [specific_tool]
            else:
                print(f"{Colors.ERROR}Unknown tool: {specific_tool}{Colors.RESET}")
                return self.report
        elif specific_category:
            category_enum = None
            for cat in ToolCategory:
                if cat.value.lower() == specific_category.lower() or cat.name.lower() == specific_category.lower():
                    category_enum = cat
                    break
            
            if category_enum:
                tools_to_test = [
                    tool for tool, cat in TOOL_CATEGORIES.items()
                    if cat == category_enum
                ]
            else:
                print(f"{Colors.ERROR}Unknown category: {specific_category}{Colors.RESET}")
                return self.report
        else:
            tools_to_test = list(TEST_QUERIES.keys())
        
        self.report.total_tools = len(tools_to_test)
        
        # Group by category for organized output
        tools_by_category: Dict[ToolCategory, List[str]] = {}
        for tool in tools_to_test:
            cat = TOOL_CATEGORIES.get(tool, ToolCategory.CORE)
            tools_by_category.setdefault(cat, []).append(tool)
        
        # Run tests by category
        total_tools_tested = 0
        for category, tools in sorted(tools_by_category.items(), key=lambda x: x[0].value):
            self.console.print_section(f"📁 {category.value}", category)
            
            for tool in sorted(tools):
                total_tools_tested += 1
                queries = TEST_QUERIES.get(tool, [])
                
                if not queries:
                    self.console.print_warning(f"No queries defined for {tool}")
                    continue
                
                self.console.print_tool_header(
                    tool, category, total_tools_tested, len(tools_to_test)
                )
                
                results = self.run_tool_tests(tool, queries, random_count)
                self.results.extend(results)
        
        # Generate summary
        self._generate_summary()
        
        return self.report
    
    def _generate_summary(self) -> None:
        """Generate test summary statistics."""
        self.report.end_time = datetime.now().isoformat()
        self.report.results = self.results
        
        # Calculate totals
        self.report.total_tests = len(self.results)
        self.report.total_passed = sum(1 for r in self.results if r.status == "success")
        self.report.total_failed = self.report.total_tests - self.report.total_passed
        
        # Calculate per-tool summaries
        tool_stats: Dict[str, ToolSummary] = {}
        for result in self.results:
            if result.tool not in tool_stats:
                tool_stats[result.tool] = ToolSummary(
                    tool=result.tool,
                    category=result.category
                )
            
            summary = tool_stats[result.tool]
            summary.total_tests += 1
            
            if result.status == "success":
                summary.passed += 1
            elif result.status == "failure":
                summary.failed += 1
            elif result.status == "timeout":
                summary.timeout += 1
            elif result.status == "error":
                summary.error += 1
            
            summary.avg_execution_time_ms += result.execution_time_ms
        
        # Average execution times
        for summary in tool_stats.values():
            if summary.total_tests > 0:
                summary.avg_execution_time_ms /= summary.total_tests
        
        self.report.tool_summaries = list(tool_stats.values())
    
    def print_summary(self) -> None:
        """Print final summary to console."""
        self.console.print_header("TEST SUMMARY")
        
        # Overall stats
        success_rate = self.report.overall_success_rate
        rate_color = Colors.SUCCESS if success_rate >= 80 else Colors.WARNING if success_rate >= 50 else Colors.ERROR
        
        self.console.print_summary_line("Total Tools Tested", str(self.report.total_tools))
        self.console.print_summary_line("Total Tests Run", str(self.report.total_tests))
        self.console.print_summary_line("Passed", str(self.report.total_passed), Colors.SUCCESS)
        self.console.print_summary_line("Failed", str(self.report.total_failed), Colors.ERROR)
        self.console.print_summary_line("Success Rate", f"{success_rate:.1f}%", rate_color)
        self.console.print_summary_line("Duration", f"{self.report.duration_seconds:.1f}s")
        
        # Per-tool breakdown (if verbose)
        if self.verbose and self.report.tool_summaries:
            print(f"\n{Colors.BOLD}Per-Tool Results:{Colors.RESET}")
            for summary in sorted(self.report.tool_summaries, key=lambda x: x.success_rate):
                color = CATEGORY_COLORS.get(
                    ToolCategory(summary.category), Colors.INFO
                )
                status = "✓" if summary.success_rate >= 80 else "⚠" if summary.success_rate >= 50 else "✗"
                print(f"  {color}{status} {summary.tool:.<35} {summary.success_rate:>5.1f}% "
                      f"({summary.passed}/{summary.total_tests}){Colors.RESET}")
    
    def save_results(self, output_dir: Optional[Path] = None) -> tuple[Path, Path]:
        """Save results to JSON and text report files."""
        if output_dir is None:
            output_dir = PROJECT_ROOT / "test_results"
        
        output_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save JSON results
        json_path = output_dir / f"test_results_{timestamp}.json"
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
                        "tool": s.tool,
                        "category": s.category,
                        "total_tests": s.total_tests,
                        "passed": s.passed,
                        "failed": s.failed,
                        "timeout": s.timeout,
                        "error": s.error,
                        "success_rate": s.success_rate,
                        "avg_execution_time_ms": s.avg_execution_time_ms,
                    }
                    for s in self.report.tool_summaries
                ],
                "detailed_results": [r.to_dict() for r in self.results]
            }, f, indent=2, default=str)
        
        # Save text report
        report_path = output_dir / f"test_report_{timestamp}.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("="*70 + "\n")
            f.write("GMAIL TOOLS TEST REPORT\n".center(70) + "\n")
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
            for summary in sorted(self.report.tool_summaries, key=lambda x: x.tool):
                f.write(f"\n{summary.tool} ({summary.category})\n")
                f.write(f"  Tests:    {summary.total_tests}\n")
                f.write(f"  Passed:   {summary.passed}\n")
                f.write(f"  Failed:   {summary.failed}\n")
                f.write(f"  Timeout:  {summary.timeout}\n")
                f.write(f"  Error:    {summary.error}\n")
                f.write(f"  Success:  {summary.success_rate:.1f}%\n")
                f.write(f"  Avg Time: {summary.avg_execution_time_ms:.0f}ms\n")
            
            f.write("\n" + "="*70 + "\n")
            f.write("DETAILED RESULTS\n")
            f.write("="*70 + "\n\n")
            
            for result in self.results:
                f.write(f"\n[{result.status.upper()}] {result.tool}\n")
                f.write(f"  Query:    {result.query}\n")
                f.write(f"  Selected: {result.selected_tool or 'N/A'}\n")
                f.write(f"  Time:     {result.execution_time_ms:.0f}ms\n")
                if result.error:
                    f.write(f"  Error:    {result.error[:200]}\n")
                f.write(f"  Response: {result.response[:200]}...\n")
                f.write("-" * 40 + "\n")
        
        return json_path, report_path


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Comprehensive test suite for Gmail tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_gmail_tools.py                      # Test all tools
  python test_gmail_tools.py --tool send_email    # Test specific tool
  python test_gmail_tools.py --category core      # Test category
  python test_gmail_tools.py --random 5           # 5 random queries per tool
  python test_gmail_tools.py --verbose            # Detailed output
        """
    )
    
    parser.add_argument(
        "--tool",
        type=str,
        help="Test only this specific tool"
    )
    parser.add_argument(
        "--category",
        type=str,
        choices=[c.name.lower() for c in ToolCategory] + [c.value.lower() for c in ToolCategory],
        help="Test only tools in this category"
    )
    parser.add_argument(
        "--random",
        type=int,
        metavar="N",
        help="Randomly select N queries per tool (default: all)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Timeout per test in seconds (default: 30)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for output files (default: ./test_results)"
    )
    
    args = parser.parse_args()
    
    # Create and run tester
    tester = GmailToolTester(
        verbose=args.verbose,
        timeout=args.timeout
    )
    
    try:
        # Run tests
        report = tester.run_all_tests(
            specific_tool=args.tool,
            specific_category=args.category,
            random_count=args.random
        )
        
        # Print summary
        tester.print_summary()
        
        # Save results
        json_path, report_path = tester.save_results(args.output_dir)
        
        print(f"\n{Colors.BOLD}{Colors.INFO}Results saved:{Colors.RESET}")
        print(f"  JSON:   {json_path}")
        print(f"  Report: {report_path}")
        
        # Exit with appropriate code
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
