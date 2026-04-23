#!/usr/bin/env python3
"""
Google Drive Tools Test Suite with REAL DATA
============================================

Tests ALL Google Drive tools using actual data from your Google Drive.

Prerequisites:
    1. Run 'python auth.py' to authenticate first
    2. Have some files/folders in your Google Drive

Usage:
    python test_drive_tools_real_data.py [--tool TOOL_NAME] [--category CATEGORY] [--random N] [--verbose]

Examples:
    python test_drive_tools_real_data.py                      # Test all tools
    python test_drive_tools_real_data.py --tool list_files   # Test only list_files
    python test_drive_tools_real_data.py --random 5          # 5 random queries per tool

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
from enum import Enum

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.main import build_server
from app.core.orchestrator import run_agent
from app.integrations.drive.registry import DRIVE_TOOLS
from app.integrations.drive.core import (
    authenticate_drive,
    list_files,
    list_folders,
    get_starred_files,
    get_recent_files,
    search_files,
    get_file_metadata,
    _api_call,
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
    BROWSE = "\033[96m"
    SEARCH = "\033[95m"
    ORGANIZE = "\033[94m"
    SHARE = "\033[93m"


class ToolCategory(Enum):
    BROWSE = "Browse & List"
    SEARCH = "Search & Metadata"
    ORGANIZE = "Organize & Manage"
    SHARE = "Sharing & Permissions"


CATEGORY_COLORS = {
    ToolCategory.BROWSE: Colors.BROWSE,
    ToolCategory.SEARCH: Colors.SEARCH,
    ToolCategory.ORGANIZE: Colors.ORGANIZE,
    ToolCategory.SHARE: Colors.SHARE,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Real Data Fetcher
# ═══════════════════════════════════════════════════════════════════════════════

class DriveDataFetcher:
    """Fetches real data from Google Drive for use in test queries."""
    
    def __init__(self, max_files: int = 15):
        self.max_files = max_files
        self._data: Optional[Dict[str, Any]] = None
    
    def _ensure_auth(self) -> bool:
        try:
            authenticate_drive()
            return True
        except PermissionError:
            return False
    
    def fetch(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        
        if not self._ensure_auth():
            print(f"{Colors.WARNING}⚠ Google Drive not authenticated. Run 'python auth.py' first.{Colors.RESET}")
            print(f"{Colors.DIM}   Falling back to dummy data.{Colors.RESET}\n")
            return self._get_fallback_data()
        
        print(f"{Colors.INFO}📁 Fetching real data from Google Drive...{Colors.RESET}")
        
        data = {
            "file_ids": [],
            "file_names": [],
            "folder_ids": [],
            "folder_names": [],
            "starred_ids": [],
            "search_terms": [],
            "file_types": [],
        }
        
        try:
            files = list_files(limit=self.max_files)
            for f in files:
                data["file_ids"].append(f.get("id"))
                data["file_names"].append(f.get("name", "Untitled"))
                if f.get("type"):
                    data["file_types"].append(f.get("type"))
            
            folders = list_folders(limit=10)
            for f in folders:
                data["folder_ids"].append(f.get("id"))
                data["folder_names"].append(f.get("name", "Untitled Folder"))
            
            starred = get_starred_files(limit=5)
            for f in starred:
                data["starred_ids"].append(f.get("id"))
            
            # Get search terms from actual file names
            if data["file_names"]:
                data["search_terms"] = [n.split()[0] for n in data["file_names"][:5] if n.split()]
            if len(data["search_terms"]) < 3:
                data["search_terms"].extend(["document", "image", "report"])
            
            self._data = data
            self._print_summary(data)
            return data
            
        except Exception as e:
            print(f"{Colors.ERROR}✗ Error fetching Drive data: {e}{Colors.RESET}")
            return self._get_fallback_data()
    
    def _get_fallback_data(self) -> Dict[str, Any]:
        return {
            "file_ids": ["file_001", "file_002", "file_003", "file_004", "file_005"],
            "file_names": ["Budget.xlsx", "Notes.docx", "Image.png", "Report.pdf", "Data.csv"],
            "folder_ids": ["folder_001", "folder_002", "folder_003"],
            "folder_names": ["Work", "Personal", "Projects"],
            "starred_ids": ["file_001", "file_003"],
            "search_terms": ["budget", "notes", "report"],
            "file_types": ["Google Sheet", "Google Doc", "PNG Image", "PDF", "CSV File"],
        }
    
    def _print_summary(self, data: Dict[str, Any]) -> None:
        print(f"  {Colors.SUCCESS}✓ Found:{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['file_ids'])} files{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['folder_ids'])} folders{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['starred_ids'])} starred{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['search_terms'])} search terms{Colors.RESET}")
    
    def get_file_id(self, index: int = 0) -> str:
        ids = self._data.get("file_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"file_{index:03d}"
    
    def get_file_name(self, index: int = 0) -> str:
        names = self._data.get("file_names", []) if self._data else []
        if names and index < len(names):
            return names[index]
        return f"File {index + 1}"
    
    def get_folder_id(self, index: int = 0) -> str:
        ids = self._data.get("folder_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"folder_{index:03d}"
    
    def get_folder_name(self, index: int = 0) -> str:
        names = self._data.get("folder_names", []) if self._data else []
        if names and index < len(names):
            return names[index]
        return f"Folder {index + 1}"
    
    def get_search_term(self, index: int = 0) -> str:
        terms = self._data.get("search_terms", []) if self._data else []
        if terms and index < len(terms):
            return terms[index]
        return "document"
    
    def get_file_type(self, index: int = 0) -> str:
        types = self._data.get("file_types", []) if self._data else []
        if types and index < len(types):
            return types[index]
        return "file"


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic Query Generator
# ═══════════════════════════════════════════════════════════════════════════════

class QueryGenerator:
    """Generates realistic test queries using real Drive data."""
    
    def __init__(self, data_fetcher: DriveDataFetcher):
        self.data = data_fetcher
    
    def _get_file_ids(self, count: int = 3) -> List[str]:
        return [self.data.get_file_id(i) for i in range(min(count, 10))]
    
    def _get_file_names(self, count: int = 3) -> List[str]:
        return [self.data.get_file_name(i) for i in range(min(count, 10))]
    
    def _get_folder_ids(self, count: int = 3) -> List[str]:
        return [self.data.get_folder_id(i) for i in range(min(count, 10))]
    
    def _get_folder_names(self, count: int = 3) -> List[str]:
        return [self.data.get_folder_name(i) for i in range(min(count, 10))]
    
    def _get_search_terms(self, count: int = 3) -> List[str]:
        return [self.data.get_search_term(i) for i in range(min(count, 10))]
    
    def generate(self, tool: str) -> List[str]:
        """Generate 8-10 realistic queries for the specified tool."""
        method = getattr(self, f"_gen_{tool}", self._gen_default)
        return method()
    
    def _gen_list_files(self) -> List[str]:
        return [
            "Show my files", "List all my files in Drive",
            "What files do I have?", "Display my Drive files",
            "Get list of my files", "Show my recent files",
            "List my Drive contents", "What files are in my Drive?",
            "Display all files", "Show available files",
        ]
    
    def _gen_list_folders(self) -> List[str]:
        return [
            "Show my folders", "List all my folders in Drive",
            "What folders do I have?", "Display my Drive folders",
            "Get list of folders", "Show my folder structure",
            "List my Drive folders", "What folders are in my Drive?",
            "Display all folders", "Show available folders",
        ]
    
    def _gen_get_folder_contents(self) -> List[str]:
        fids = self._get_folder_ids(5)
        fnames = self._get_folder_names(5)
        return [
            f"Show contents of folder {fids[0]}",
            f"List files in {fnames[0]}",
            f"What's inside folder {fids[1] if len(fids) > 1 else fids[0]}?",
            f"Get folder contents for {fnames[1] if len(fnames) > 1 else fnames[0]}",
            f"Show files in folder {fids[2] if len(fids) > 2 else fids[0]}",
            f"List contents of {fnames[2] if len(fnames) > 2 else fnames[0]}",
            f"What's in folder {fids[0]}?",
            f"Show me {fnames[0]} contents",
            f"Get files from folder {fids[1] if len(fids) > 1 else fids[0]}",
            f"Browse folder {fnames[1] if len(fnames) > 1 else fnames[0]}",
        ]
    
    def _gen_get_starred_files(self) -> List[str]:
        return [
            "Show my starred files", "List my favorites",
            "What files have I starred?", "Display my starred items",
            "Get my starred files", "Show my important files",
            "List starred items", "What did I mark as favorite?",
            "Show starred files", "Get my favorites list",
        ]
    
    def _gen_get_recent_files(self) -> List[str]:
        return [
            "Show my recent files", "List recently modified files",
            "What did I work on recently?", "Display my latest files",
            "Get recent files", "Show my most recent work",
            "List recent items", "What files were modified lately?",
            "Show latest files", "Get my recent documents",
        ]
    
    def _gen_search_files(self) -> List[str]:
        terms = self._get_search_terms(5)
        return [
            f"Search for '{terms[0]}' in Drive",
            f"Find files containing '{terms[1] if len(terms) > 1 else terms[0]}'",
            f"Look for {terms[2] if len(terms) > 2 else terms[0]} files",
            f"Search Drive for {terms[0]}",
            f"Find {terms[1] if len(terms) > 1 else terms[0]} in my files",
            f"Search files named {terms[2] if len(terms) > 2 else terms[0]}",
            f"Look up {terms[0]} in Drive",
            f"Find documents about {terms[1] if len(terms) > 1 else terms[0]}",
            f"Search for file {terms[2] if len(terms) > 2 else terms[0]}",
            f"Find {terms[0]} in my Drive",
        ]
    
    def _gen_search_files_by_type(self) -> List[str]:
        return [
            "Search for PDF files", "Find all Google Docs",
            "Show me spreadsheets", "List all images",
            "Find video files", "Search for presentations",
            "Show Google Sheets", "Find audio files",
            "List all folders", "Search for text files",
        ]
    
    def _gen_get_file_metadata(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Get metadata for file {ids[0]}",
            f"Show info about {names[0]}",
            f"Get details for file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Show file info for {names[1] if len(names) > 1 else names[0]}",
            f"Get file metadata {ids[2] if len(ids) > 2 else ids[0]}",
            f"Show details of {names[2] if len(names) > 2 else names[0]}",
            f"Get info on file {ids[0]}",
            f"Show metadata for {names[0]}",
            f"Get file {ids[1] if len(ids) > 1 else ids[0]} details",
            f"Show info about {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_get_storage_info(self) -> List[str]:
        return [
            "Show my storage usage", "How much Drive space do I have?",
            "Get storage info", "Display my quota",
            "Show Drive storage", "How much space is left?",
            "Get my storage quota", "Display storage usage",
            "Show available space", "Get Drive capacity info",
        ]
    
    def _gen_create_folder(self) -> List[str]:
        return [
            "Create a new folder called 'Work'", "Make folder 'Personal'",
            "Create folder 'Projects' in Drive", "New folder 'Archive'",
            "Make a folder called 'Backups'", "Create 'Photos' folder",
            "New folder 'Documents'", "Create folder '2024'",
            "Make folder 'Shared'", "Create 'Temp' folder",
        ]
    
    def _gen_rename_file(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Rename file {ids[0]} to 'New Name'",
            f"Change name of {names[0]} to 'Updated {names[0]}'",
            f"Rename {names[1] if len(names) > 1 else names[0]}",
            f"Change file {ids[1] if len(ids) > 1 else ids[0]} name",
            f"Rename {names[2] if len(names) > 2 else names[0]} to 'Archive'",
            f"Update name of file {ids[2] if len(ids) > 2 else ids[0]}",
            f"Rename {names[0]} to 'Final Version'",
            f"Change {ids[0]} to new name",
            f"Rename file {names[1] if len(names) > 1 else names[0]}",
            f"Update {ids[1] if len(ids) > 1 else ids[0]} name",
        ]
    
    def _gen_move_file(self) -> List[str]:
        fids = self._get_file_ids(5)
        folder_ids = self._get_folder_ids(5)
        names = self._get_file_names(5)
        folder_names = self._get_folder_names(5)
        return [
            f"Move file {fids[0]} to folder {folder_ids[0]}",
            f"Move {names[0]} to {folder_names[0]}",
            f"Put file {fids[1] if len(fids) > 1 else fids[0]} in {folder_names[1] if len(folder_names) > 1 else folder_names[0]}",
            f"Move {names[1] if len(names) > 1 else names[0]} to folder {folder_ids[1] if len(folder_ids) > 1 else folder_ids[0]}",
            f"Transfer file {fids[2] if len(fids) > 2 else fids[0]} to {folder_names[2] if len(folder_names) > 2 else folder_names[0]}",
            f"Move {names[2] if len(names) > 2 else names[0]} into {folder_names[0]}",
            f"Put {fids[0]} in folder {folder_ids[0]}",
            f"Move file to {folder_names[1] if len(folder_names) > 1 else folder_names[0]}",
            f"Transfer {names[0]} to folder",
            f"Move {fids[1] if len(fids) > 1 else fids[0]} to {folder_names[0]}",
        ]
    
    def _gen_copy_file(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Copy file {ids[0]}",
            f"Make a copy of {names[0]}",
            f"Duplicate file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Copy {names[1] if len(names) > 1 else names[0]}",
            f"Create copy of {ids[2] if len(ids) > 2 else ids[0]}",
            f"Duplicate {names[2] if len(names) > 2 else names[0]}",
            f"Copy {names[0]} to new file",
            f"Clone file {ids[0]}",
            f"Make duplicate of {names[1] if len(names) > 1 else names[0]}",
            f"Copy file {ids[1] if len(ids) > 1 else ids[0]} as new",
        ]
    
    def _gen_trash_file(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Move file {ids[0]} to trash",
            f"Trash {names[0]}",
            f"Delete file {ids[1] if len(ids) > 1 else ids[0]} to trash",
            f"Move {names[1] if len(names) > 1 else names[0]} to trash",
            f"Trash file {ids[2] if len(ids) > 2 else ids[0]}",
            f"Put {names[2] if len(names) > 2 else names[0]} in trash",
            f"Move {ids[0]} to trash",
            f"Trash {names[0]}",
            f"Delete {ids[1] if len(ids) > 1 else ids[0]} (move to trash)",
            f"Trash file {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_restore_file(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Restore file {ids[0]} from trash",
            f"Recover {names[0]} from trash",
            f"Restore {ids[1] if len(ids) > 1 else ids[0]} from trash",
            f"Undelete {names[1] if len(names) > 1 else names[0]}",
            f"Restore file {ids[2] if len(ids) > 2 else ids[0]}",
            f"Recover {names[2] if len(names) > 2 else names[0]} from trash",
            f"Restore {ids[0]}",
            f"Undelete file {names[0]}",
            f"Recover {ids[1] if len(ids) > 1 else ids[0]} from trash",
            f"Restore {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_delete_file(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Permanently delete file {ids[0]}",
            f"Erase {names[0]} forever",
            f"Delete {ids[1] if len(ids) > 1 else ids[0]} permanently",
            f"Permanently remove {names[1] if len(names) > 1 else names[0]}",
            f"Erase file {ids[2] if len(ids) > 2 else ids[0]}",
            f"Permanently delete {names[2] if len(names) > 2 else names[0]}",
            f"Delete {ids[0]} forever",
            f"Erase {names[0]} permanently",
            f"Permanently remove file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Delete {names[1] if len(names) > 1 else names[0]} permanently",
        ]
    
    def _gen_get_file_permissions(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Show permissions for file {ids[0]}",
            f"Who can access {names[0]}?",
            f"Get permissions of {ids[1] if len(ids) > 1 else ids[0]}",
            f"Check sharing settings for {names[1] if len(names) > 1 else names[0]}",
            f"Show access rights for file {ids[2] if len(ids) > 2 else ids[0]}",
            f"View permissions of {names[2] if len(names) > 2 else names[0]}",
            f"Who has access to {ids[0]}?",
            f"Check {names[0]} permissions",
            f"Get sharing info for file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Show access for {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_share_file(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Share file {ids[0]} with someone",
            f"Share {names[0]} with editor access",
            f"Give access to file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Share {names[1] if len(names) > 1 else names[0]} with viewer",
            f"Grant access to {ids[2] if len(ids) > 2 else ids[0]}",
            f"Share {names[2] if len(names) > 2 else names[0]} with commenter",
            f"Allow access to file {ids[0]}",
            f"Share {names[0]} with user@example.com",
            f"Give {ids[1] if len(ids) > 1 else ids[0]} access to someone",
            f"Share file {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_share_file_publicly(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Make file {ids[0]} public",
            f"Share {names[0]} publicly",
            f"Make {ids[1] if len(ids) > 1 else ids[0]} accessible to anyone",
            f"Public share {names[1] if len(names) > 1 else names[0]}",
            f"Make file {ids[2] if len(ids) > 2 else ids[0]} public",
            f"Share {names[2] if len(names) > 2 else names[0]} with everyone",
            f"Make {ids[0]} publicly accessible",
            f"Create public link for {names[0]}",
            f"Share file {ids[1] if len(ids) > 1 else ids[0]} with the world",
            f"Make {names[1] if len(names) > 1 else names[0]} public",
        ]
    
    def _gen_get_shareable_link(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Get shareable link for {ids[0]}",
            f"Create link to share {names[0]}",
            f"Generate link for file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Get link for {names[1] if len(names) > 1 else names[0]}",
            f"Create shareable link for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Generate sharing link for {names[2] if len(names) > 2 else names[0]}",
            f"Get URL to share {names[0]}",
            f"Create link for file {ids[0]}",
            f"Get share link for {names[1] if len(names) > 1 else names[0]}",
            f"Generate link to {ids[1] if len(ids) > 1 else ids[0]}",
        ]
    
    def _gen_remove_permission(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Remove permission from file {ids[0]}",
            f"Revoke access to {names[0]}",
            f"Remove user access from {ids[1] if len(ids) > 1 else ids[0]}",
            f"Delete permission for {names[1] if len(names) > 1 else names[0]}",
            f"Remove access from file {ids[2] if len(ids) > 2 else ids[0]}",
            f"Revoke {names[2] if len(names) > 2 else names[0]} access",
            f"Remove sharing permission from {ids[0]}",
            f"Delete access for {names[0]}",
            f"Remove user from file {ids[1] if len(ids) > 1 else ids[0]}",
            f"Revoke permission for {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_remove_access(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Remove all access from file {ids[0]}",
            f"Revoke all access to {names[0]}",
            f"Remove everyone from {ids[1] if len(ids) > 1 else ids[0]}",
            f"Delete all sharing for {names[1] if len(names) > 1 else names[0]}",
            f"Remove all permissions from {ids[2] if len(ids) > 2 else ids[0]}",
            f"Revoke all access to {names[2] if len(names) > 2 else names[0]}",
            f"Delete all sharing from file {ids[0]}",
            f"Remove all users from {names[0]}",
            f"Clear all access from {ids[1] if len(ids) > 1 else ids[0]}",
            f"Revoke all permissions for {names[1] if len(names) > 1 else names[0]}",
        ]
    
    def _gen_make_file_private(self) -> List[str]:
        ids = self._get_file_ids(5)
        names = self._get_file_names(5)
        return [
            f"Make file {ids[0]} private",
            f"Set {names[0]} to private",
            f"Make {ids[1] if len(ids) > 1 else ids[0]} private only to me",
            f"Private {names[1] if len(names) > 1 else names[0]}",
            f"Restrict access to file {ids[2] if len(ids) > 2 else ids[0]}",
            f"Make {names[2] if len(names) > 2 else names[0]} private",
            f"Remove public access from {ids[0]}",
            f"Set {names[0]} to private only",
            f"Make file {ids[1] if len(ids) > 1 else ids[0]} private",
            f"Private {names[1] if len(names) > 1 else names[0]} only",
        ]
    
    def _gen_upload_file(self) -> List[str]:
        return [
            "Upload a file to Drive", "Upload document.pdf",
            "Add file to my Drive", "Upload image.png",
            "Upload new file", "Add document to Drive",
            "Upload spreadsheet.xlsx", "Upload file to folder",
            "Add new file to Drive", "Upload presentation.pptx",
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
# Tool Categories
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_CATEGORIES = {
    "list_files": ToolCategory.BROWSE,
    "list_folders": ToolCategory.BROWSE,
    "get_folder_contents": ToolCategory.BROWSE,
    "get_starred_files": ToolCategory.BROWSE,
    "get_recent_files": ToolCategory.BROWSE,
    "search_files": ToolCategory.SEARCH,
    "search_files_by_type": ToolCategory.SEARCH,
    "get_file_metadata": ToolCategory.SEARCH,
    "get_storage_info": ToolCategory.SEARCH,
    "create_folder": ToolCategory.ORGANIZE,
    "rename_file": ToolCategory.ORGANIZE,
    "move_file": ToolCategory.ORGANIZE,
    "copy_file": ToolCategory.ORGANIZE,
    "trash_file": ToolCategory.ORGANIZE,
    "restore_file": ToolCategory.ORGANIZE,
    "delete_file": ToolCategory.ORGANIZE,
    "get_file_permissions": ToolCategory.SHARE,
    "share_file": ToolCategory.SHARE,
    "share_file_publicly": ToolCategory.SHARE,
    "get_shareable_link": ToolCategory.SHARE,
    "remove_permission": ToolCategory.SHARE,
    "remove_access": ToolCategory.SHARE,
    "make_file_private": ToolCategory.SHARE,
    "upload_file": ToolCategory.ORGANIZE,
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


class DriveToolTester:
    DEFAULT_TIMEOUT = 30
    
    def __init__(self, verbose: bool = False, timeout: int = DEFAULT_TIMEOUT, max_files: int = 15):
        self.verbose = verbose
        self.timeout = timeout
        self.results: List[TestResult] = []
        self.report = TestReport(
            start_time=datetime.now().isoformat(),
            end_time=""
        )
        
        # Initialize data fetcher and query generator
        self.data_fetcher = DriveDataFetcher(max_files=max_files)
        self.query_generator = QueryGenerator(self.data_fetcher)
        
        # Initialize MCP server and monitor
        self.mcp = build_server()
        self.monitor = ToolMonitor(self.mcp)
        self.state: Dict[str, Any] = {}
        
        # Fetch real data
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
        category = TOOL_CATEGORIES.get(tool, ToolCategory.BROWSE)
        self.state = {}
        self.monitor.reset()
        
        try:
            response = run_agent(query, self.monitor, self.state, mode="drive")
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
        print(f"{Colors.BOLD}{Colors.INFO}{'DRIVE TOOLS TEST SUITE (REAL DATA)'.center(70)}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}\n")
        
        available_tools = [t for t in TOOL_CATEGORIES.keys() if t in DRIVE_TOOLS]
        
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
        
        by_category: Dict[ToolCategory, List[str]] = {}
        for tool in tools_to_test:
            cat = TOOL_CATEGORIES.get(tool, ToolCategory.BROWSE)
            by_category.setdefault(cat, []).append(tool)
        
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
        
        json_path = output_dir / f"drive_test_results_{ts}.json"
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
        
        report_path = output_dir / f"drive_test_report_{ts}.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("="*70 + "\n")
            f.write("DRIVE TOOLS TEST REPORT (REAL DATA)\n".center(70) + "\n")
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
        description="Google Drive Tools Test Suite with Real Data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_drive_tools_real_data.py              # Test all tools
  python test_drive_tools_real_data.py --tool list_files    # Test specific tool
  python test_drive_tools_real_data.py --category browse    # Test category
  python test_drive_tools_real_data.py --random 3   # 3 random queries per tool
        """
    )
    
    parser.add_argument("--tool", type=str, help="Test only this specific tool")
    parser.add_argument("--category", type=str,
                       choices=[c.name.lower() for c in ToolCategory] + [c.value.lower() for c in ToolCategory],
                       help="Test only tools in this category")
    parser.add_argument("--random", type=int, metavar="N", help="Randomly select N queries per tool")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per test in seconds")
    parser.add_argument("--max-files", type=int, default=15, help="Max files to fetch for real data")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for output files")
    
    args = parser.parse_args()
    
    tester = DriveToolTester(
        verbose=args.verbose,
        timeout=args.timeout,
        max_files=args.max_files
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
