#!/usr/bin/env python3
"""
Google Docs Tools Test Suite with REAL DATA
===========================================

Tests ALL Google Docs tools using actual data from your Google Drive/Docs.

Prerequisites:
    1. Run 'python auth.py' to authenticate first
    2. Have some Google Docs in your Drive

Usage:
    python test_docs_tools_real_data.py [--tool TOOL_NAME] [--category CATEGORY] [--random N] [--verbose]

Examples:
    python test_docs_tools_real_data.py                      # Test all tools
    python test_docs_tools_real_data.py --tool get_doc      # Test only get_doc
    python test_docs_tools_real_data.py --random 5          # 5 random queries per tool

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
from app.integrations.docs.registry import DOCS_TOOLS
from app.integrations.docs.core import (
    authenticate_docs,
    list_docs,
    search_docs,
    get_doc,
    get_doc_content,
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
    READ = "\033[95m"
    WRITE = "\033[94m"
    MANAGE = "\033[93m"


class ToolCategory(Enum):
    BROWSE = "Browse & Search"
    READ = "Read Operations"
    WRITE = "Write Operations"
    MANAGE = "Document Management"


CATEGORY_COLORS = {
    ToolCategory.BROWSE: Colors.BROWSE,
    ToolCategory.READ: Colors.READ,
    ToolCategory.WRITE: Colors.WRITE,
    ToolCategory.MANAGE: Colors.MANAGE,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Real Data Fetcher
# ═══════════════════════════════════════════════════════════════════════════════

class DocsDataFetcher:
    """Fetches real data from Google Docs for use in test queries."""
    
    def __init__(self, max_docs: int = 10):
        self.max_docs = max_docs
        self._data: Optional[Dict[str, Any]] = None
    
    def _ensure_auth(self) -> bool:
        try:
            authenticate_docs()
            return True
        except PermissionError:
            return False
    
    def fetch(self) -> Dict[str, Any]:
        if self._data is not None:
            return self._data
        
        if not self._ensure_auth():
            print(f"{Colors.WARNING}⚠ Google Docs not authenticated. Run 'python auth.py' first.{Colors.RESET}")
            print(f"{Colors.DIM}   Falling back to dummy data.{Colors.RESET}\n")
            return self._get_fallback_data()
        
        print(f"{Colors.INFO}📄 Fetching real data from Google Docs...{Colors.RESET}")
        
        data = {
            "doc_ids": [],
            "doc_titles": [],
            "search_terms": [],
            "content_snippets": [],
        }
        
        try:
            docs = list_docs(limit=self.max_docs)
            for doc in docs:
                data["doc_ids"].append(doc.get("id"))
                title = doc.get("title", "Untitled")
                data["doc_titles"].append(title)
            
            # Get search terms from actual titles
            if data["doc_titles"]:
                data["search_terms"] = [t.split()[0] for t in data["doc_titles"][:5] if t.split()]
            if len(data["search_terms"]) < 3:
                data["search_terms"].extend(["meeting", "notes", "report"])
            
            # Try to get content snippets from first doc
            if data["doc_ids"]:
                try:
                    content = get_doc_content(data["doc_ids"][0])
                    text = content.get("text", "")
                    if text:
                        # Extract first 50 chars as snippet
                        data["content_snippets"].append(text[:50])
                except Exception:
                    pass
            
            if not data["content_snippets"]:
                data["content_snippets"] = ["This is sample document content.", "Meeting notes from last week.", "Project plan details."]
            
            self._data = data
            self._print_summary(data)
            return data
            
        except Exception as e:
            print(f"{Colors.ERROR}✗ Error fetching Docs data: {e}{Colors.RESET}")
            return self._get_fallback_data()
    
    def _get_fallback_data(self) -> Dict[str, Any]:
        return {
            "doc_ids": ["doc_001", "doc_002", "doc_003"],
            "doc_titles": ["Meeting Notes", "Project Proposal", "Report 2024"],
            "search_terms": ["meeting", "project", "report"],
            "content_snippets": ["Meeting notes from last week.", "Project plan details.", "Annual report content."],
        }
    
    def _print_summary(self, data: Dict[str, Any]) -> None:
        print(f"  {Colors.SUCCESS}✓ Found:{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['doc_ids'])} documents{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['doc_titles'])} titles{Colors.RESET}")
        print(f"    {Colors.DIM}- {len(data['search_terms'])} search terms{Colors.RESET}")
    
    def get_doc_id(self, index: int = 0) -> str:
        ids = self._data.get("doc_ids", []) if self._data else []
        if ids and index < len(ids):
            return ids[index]
        return f"doc_{index:03d}"
    
    def get_doc_title(self, index: int = 0) -> str:
        titles = self._data.get("doc_titles", []) if self._data else []
        if titles and index < len(titles):
            return titles[index]
        return f"Document {index + 1}"
    
    def get_search_term(self, index: int = 0) -> str:
        terms = self._data.get("search_terms", []) if self._data else []
        if terms and index < len(terms):
            return terms[index]
        return "document"
    
    def get_content_snippet(self, index: int = 0) -> str:
        snippets = self._data.get("content_snippets", []) if self._data else []
        if snippets and index < len(snippets):
            return snippets[index]
        return "sample content"


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic Query Generator
# ═══════════════════════════════════════════════════════════════════════════════

class QueryGenerator:
    """Generates realistic test queries using real Docs data."""
    
    def __init__(self, data_fetcher: DocsDataFetcher):
        self.data = data_fetcher
    
    def _get_ids(self, count: int = 3) -> List[str]:
        return [self.data.get_doc_id(i) for i in range(min(count, 10))]
    
    def _get_titles(self, count: int = 3) -> List[str]:
        return [self.data.get_doc_title(i) for i in range(min(count, 10))]
    
    def _get_search_terms(self, count: int = 3) -> List[str]:
        return [self.data.get_search_term(i) for i in range(min(count, 10))]
    
    def _get_snippets(self, count: int = 3) -> List[str]:
        return [self.data.get_content_snippet(i) for i in range(min(count, 10))]
    
    def generate(self, tool: str) -> List[str]:
        """Generate 8-10 realistic queries for the specified tool."""
        method = getattr(self, f"_gen_{tool}", self._gen_default)
        return method()
    
    def _gen_list_docs(self) -> List[str]:
        return [
            "Show my documents", "List all my Google Docs",
            "What documents do I have?", "Display my docs",
            "Get list of my documents", "Show my Google Docs",
            "List my docs", "What docs are in my Drive?",
            "Display all documents", "Show available documents",
        ]
    
    def _gen_search_docs(self) -> List[str]:
        terms = self._get_search_terms(5)
        return [
            f"Search docs for '{terms[0]}'",
            f"Find document containing '{terms[1] if len(terms) > 1 else terms[0]}'",
            f"Search for doc with '{terms[2] if len(terms) > 2 else terms[0]}'",
            f"Find documents about {terms[0]}",
            f"Search my docs for {terms[1] if len(terms) > 1 else terms[0]}",
            f"Look for doc named {terms[2] if len(terms) > 2 else terms[0]}",
            f"Find doc with {terms[0]} in title",
            f"Search for {terms[1] if len(terms) > 1 else terms[0]} document",
            f"Find my {terms[2] if len(terms) > 2 else terms[0]} doc",
            f"Search docs containing {terms[0]}",
        ]
    
    def _gen_get_doc(self) -> List[str]:
        ids = self._get_ids(5)
        titles = self._get_titles(5)
        return [
            f"Get info about document {ids[0]}",
            f"Show details for doc {ids[1] if len(ids) > 1 else ids[0]}",
            f"Get metadata for {titles[0]}",
            f"Show doc info for {ids[2] if len(ids) > 2 else ids[0]}",
            f"Get document {titles[1] if len(titles) > 1 else titles[0]} details",
            f"Show document {ids[0]} metadata",
            f"Get info on document {ids[1] if len(ids) > 1 else ids[0]}",
            f"Display details for {titles[2] if len(titles) > 2 else titles[0]}",
            f"Show document {ids[0]} info",
            f"Get doc {titles[0]} info",
        ]
    
    def _gen_get_doc_content(self) -> List[str]:
        ids = self._get_ids(5)
        titles = self._get_titles(5)
        return [
            f"Read document {ids[0]}",
            f"Show content of doc {ids[1] if len(ids) > 1 else ids[0]}",
            f"Get text from document {ids[2] if len(ids) > 2 else ids[0]}",
            f"Read {titles[0]} content",
            f"Show me doc {ids[0]} content",
            f"Get content of {titles[1] if len(titles) > 1 else titles[0]}",
            f"Display text from {ids[1] if len(ids) > 1 else ids[0]}",
            f"Read the document {titles[2] if len(titles) > 2 else titles[0]}",
            f"Show contents of doc {ids[0]}",
            f"Get full text from document {ids[1] if len(ids) > 1 else ids[0]}",
        ]
    
    def _gen_create_doc(self) -> List[str]:
        return [
            "Create a new document called 'Meeting Notes'",
            "Make a new doc named 'Project Plan'",
            "Create document 'Report 2024'",
            "New doc called 'Ideas Draft'",
            "Create a document named 'Summary'",
            "Make new Google Doc 'Budget Overview'",
            "Create doc 'Research Notes'",
            "New document 'Task List'",
            "Create 'Q4 Review' doc",
            "Make a doc called 'Draft Proposal'",
        ]
    
    def _gen_append_to_doc(self) -> List[str]:
        ids = self._get_ids(5)
        titles = self._get_titles(5)
        snippets = self._get_snippets(3)
        return [
            f"Add text to document {ids[0]}",
            f"Append content to doc {ids[1] if len(ids) > 1 else ids[0]}",
            f"Write '{snippets[0][:30]}...' to {titles[0]}",
            f"Add to document {ids[2] if len(ids) > 2 else ids[0]}",
            f"Append to {titles[1] if len(titles) > 1 else titles[0]}",
            f"Insert text into doc {ids[0]}",
            f"Add content to {titles[2] if len(titles) > 2 else titles[0]}",
            f"Write more in document {ids[1] if len(ids) > 1 else ids[0]}",
            f"Append text to {titles[0]}",
            f"Add to the end of doc {ids[0]}",
        ]
    
    def _gen_replace_text_in_doc(self) -> List[str]:
        ids = self._get_ids(5)
        titles = self._get_titles(5)
        return [
            f"Replace text in document {ids[0]}",
            f"Find and replace in doc {ids[1] if len(ids) > 1 else ids[0]}",
            f"Update content in {titles[0]}",
            f"Replace 'old' with 'new' in document {ids[2] if len(ids) > 2 else ids[0]}",
            f"Change text in {titles[1] if len(titles) > 1 else titles[0]}",
            f"Find 'meeting' replace with 'call' in doc {ids[0]}",
            f"Update {titles[2] if len(titles) > 2 else titles[0]} content",
            f"Replace text in {ids[1] if len(ids) > 1 else ids[0]}",
            f"Modify content of document {titles[0]}",
            f"Update text in doc {ids[0]}",
        ]
    
    def _gen_update_doc_title(self) -> List[str]:
        ids = self._get_ids(5)
        titles = self._get_titles(5)
        return [
            f"Rename document {ids[0]} to 'New Title'",
            f"Change title of doc {ids[1] if len(ids) > 1 else ids[0]}",
            f"Update {titles[0]} name to 'Updated {titles[0]}'",
            f"Rename {titles[1] if len(titles) > 1 else titles[0]}",
            f"Change document {ids[0]} title",
            f"Rename doc to 'Final Version'",
            f"Update title of {titles[2] if len(titles) > 2 else titles[0]}",
            f"Change name of document {ids[1] if len(ids) > 1 else ids[0]}",
            f"Rename {titles[0]} to 'Archive'",
            f"Update doc {ids[0]} title",
        ]
    
    def _gen_delete_doc(self) -> List[str]:
        ids = self._get_ids(5)
        titles = self._get_titles(5)
        return [
            f"Delete document {ids[0]}",
            f"Remove doc {ids[1] if len(ids) > 1 else ids[0]}",
            f"Delete {titles[0]} document",
            f"Trash doc {ids[2] if len(ids) > 2 else ids[0]}",
            f"Remove document {titles[1] if len(titles) > 1 else titles[0]}",
            f"Delete Google Doc {ids[0]}",
            f"Move {ids[1] if len(ids) > 1 else ids[0]} to trash",
            f"Remove doc {titles[2] if len(titles) > 2 else titles[0]}",
            f"Delete document with ID {ids[0]}",
            f"Trash {ids[1] if len(ids) > 1 else ids[0]}",
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
    "list_docs": ToolCategory.BROWSE,
    "search_docs": ToolCategory.BROWSE,
    "get_doc": ToolCategory.READ,
    "get_doc_content": ToolCategory.READ,
    "create_doc": ToolCategory.WRITE,
    "append_to_doc": ToolCategory.WRITE,
    "replace_text_in_doc": ToolCategory.WRITE,
    "update_doc_title": ToolCategory.MANAGE,
    "delete_doc": ToolCategory.MANAGE,
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


class DocsToolTester:
    DEFAULT_TIMEOUT = 30
    
    def __init__(self, verbose: bool = False, timeout: int = DEFAULT_TIMEOUT, max_docs: int = 10):
        self.verbose = verbose
        self.timeout = timeout
        self.results: List[TestResult] = []
        self.report = TestReport(
            start_time=datetime.now().isoformat(),
            end_time=""
        )
        
        # Initialize data fetcher and query generator
        self.data_fetcher = DocsDataFetcher(max_docs=max_docs)
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
            response = run_agent(query, self.monitor, self.state, mode="docs")
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
        print(f"{Colors.BOLD}{Colors.INFO}{'DOCS TOOLS TEST SUITE (REAL DATA)'.center(70)}{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.INFO}{'='*70}{Colors.RESET}\n")
        
        available_tools = [t for t in TOOL_CATEGORIES.keys() if t in DOCS_TOOLS]
        
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
        
        json_path = output_dir / f"docs_test_results_{ts}.json"
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
        
        report_path = output_dir / f"docs_test_report_{ts}.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("="*70 + "\n")
            f.write("DOCS TOOLS TEST REPORT (REAL DATA)\n".center(70) + "\n")
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
        description="Google Docs Tools Test Suite with Real Data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_docs_tools_real_data.py              # Test all tools
  python test_docs_tools_real_data.py --tool get_doc    # Test specific tool
  python test_docs_tools_real_data.py --category read   # Test category
  python test_docs_tools_real_data.py --random 3   # 3 random queries per tool
        """
    )
    
    parser.add_argument("--tool", type=str, help="Test only this specific tool")
    parser.add_argument("--category", type=str,
                       choices=[c.name.lower() for c in ToolCategory] + [c.value.lower() for c in ToolCategory],
                       help="Test only tools in this category")
    parser.add_argument("--random", type=int, metavar="N", help="Randomly select N queries per tool")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout per test in seconds")
    parser.add_argument("--max-docs", type=int, default=10, help="Max docs to fetch for real data")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for output files")
    
    args = parser.parse_args()
    
    tester = DocsToolTester(
        verbose=args.verbose,
        timeout=args.timeout,
        max_docs=args.max_docs
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
