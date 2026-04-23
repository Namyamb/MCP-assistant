# G-Assistant: Local AI Gmail & Google Workspace Agent

## Project Overview

**Name:** gmail-mcp-agent  
**Version:** 0.1.0  
**Description:** A local AI Gmail Assistant with UI that integrates multiple Google Workspace services (Gmail, Drive, Docs, Sheets) via the Model Context Protocol (MCP).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         G-Assistant                              │
│                    (Local AI Workspace Agent)                     │
├─────────────────────────────────────────────────────────────────┤
│  UI Layer (Web/Desktop)                                         │
│  ├── Web Interface (app/ui/web.py)                               │
│  ├── Static Assets (HTML/CSS/JS)                                  │
│  └── Desktop Interface (app/ui/desktop.py)                        │
├─────────────────────────────────────────────────────────────────┤
│  Agent Orchestrator (app/core/orchestrator.py)                  │
│  ├── Context State Management                                     │
│  ├── Intent Detection (Strict Path)                             │
│  ├── LLM Resolution & Tool Execution                            │
│  └── Multi-Pass Compound Command Handling                        │
├─────────────────────────────────────────────────────────────────┤
│  MCP Server (app/core/mcp.py)                                     │
│  └── Tool Registration & Execution                              │
├─────────────────────────────────────────────────────────────────┤
│  Integration Layer                                                │
│  ├── Gmail MCP (50+ tools)                                      │
│  ├── Drive MCP (30+ tools)                                        │
│  ├── Docs MCP (30+ tools)                                        │
│  └── Sheets MCP (30+ tools)                                       │
├─────────────────────────────────────────────────────────────────┤
│  Core Infrastructure                                              │
│  ├── LLM Client (LM Studio compatible)                            │
│  ├── Configuration & Auth                                         │
│  └── Utilities (Cache, Logging, Errors)                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Project Structure

```
mcp-project/
├── app/
│   ├── core/                        # Core infrastructure
│   │   ├── config.py               # Configuration & constants
│   │   ├── llm_client.py          # LLM API client (LM Studio)
│   │   ├── mcp.py                 # MCP server implementation
│   │   └── orchestrator.py        # Agent orchestration logic (2729 lines)
│   ├── integrations/               # Google Workspace integrations
│   │   ├── gmail/                 # Gmail MCP (50+ tools)
│   │   │   ├── core.py          # V1 core operations (719 lines)
│   │   │   ├── core_v2.py       # V2 unified tools (36K lines)
│   │   │   ├── ai.py            # AI analysis tools (121 lines)
│   │   │   ├── registry.py      # Tool registration (116 lines)
│   │   │   └── utils.py         # Utilities & errors (21K lines)
│   │   ├── docs/                # Google Docs MCP (30+ tools)
│   │   │   ├── core.py          # V1 core
│   │   │   ├── core_v2.py       # V2 unified tools
│   │   │   ├── registry.py      # Tool registration
│   │   │   └── utils.py         # Utilities
│   │   ├── sheets/              # Google Sheets MCP (30+ tools)
│   │   │   ├── core.py          # V1 core
│   │   │   ├── core_v2.py       # V2 unified tools
│   │   │   ├── registry.py      # Tool registration
│   │   │   └── utils.py         # Utilities
│   │   └── drive/               # Google Drive MCP (30+ tools)
│   │       ├── README.md        # Drive v2 documentation
│   │       ├── core.py          # V1 core
│   │       ├── core_v2.py       # V2 unified tools
│   │       ├── registry.py      # Tool registration
│   │       └── utils.py         # Utilities
│   ├── tools/                    # General utility tools
│   │   ├── calculator.py        # Math expression evaluator
│   │   └── filesystem.py        # File operations
│   ├── ui/                       # User interface
│   │   ├── web.py               # Web server (HTTP API)
│   │   ├── desktop.py           # Desktop interface stub
│   │   └── static/              # Web assets
│   │       ├── index.html       # Main HTML (128 lines)
│   │       └── assets/
│   │           ├── app.css      # Styles (14K)
│   │           └── app.js       # Frontend logic (15K)
│   └── main.py                   # Application entry point
├── credentials/                  # Auth credentials storage
├── data/                         # Application data
│   ├── downloads/               # Downloaded files
│   ├── uploads/                 # Uploaded files
│   └── *.json                   # Cache & store files
├── tests/                        # Test suites
│   ├── test_gmail_tools.py
│   ├── test_gmail_tools_real_data.py
│   ├── test_drive_tools_real_data.py
│   ├── test_docs_tools_real_data.py
│   └── test_sheets_tools_real_data.py
├── main.py                       # CLI entry point
├── pyproject.toml               # Project configuration
├── gmail_test_questions.md      # Test scenarios (285 lines)
└── CLAUDE.md                    # Graphify instructions
```

---

## Core Components

### 1. MCP Server (`app/core/mcp.py`)

- **Purpose:** Tool registration and execution hub
- **Key Features:**
  - Dynamic tool registration
  - Standardized error handling
  - Response formatting with `{"success": bool, "result/error": ...}`

### 2. Orchestrator (`app/core/orchestrator.py`)

- **Purpose:** Main agent logic for intent detection and LLM coordination
- **Size:** 2,729 lines
- **Key Features:**
  - **Context State Management:** Tracks entities, recent actions, conversation history
  - **Strict Intent Detection:** Regex-based shortcuts for common commands
  - **LLM Resolution:** Natural language → tool calls via LM Studio
  - **Compound Commands:** Multi-step execution with context updates between steps
  - **Safety Layer:** Context injection for missing arguments
  - **Response Parsing:** Handles JSON, proprietary LM Studio format, and malformed responses

**ContextState Dataclass:**
- `entities`: Current primary entities per type (email, doc, sheet, file, folder)
- `recent_entities`: Recency stack (max 10)
- `last_viewed_ids`: Ordered list of recently viewed email IDs
- `last_email_list`: Structured email metadata for LLM context
- `history`: Per-mode conversation history

### 3. LLM Client (`app/core/llm_client.py`)

- **Purpose:** Interface to local LLM (LM Studio)
- **Configuration:**
  - URL: `http://127.0.0.1:1234/v1/chat/completions`
  - Model: `gemma-4-e2b-it` (configurable)
  - Timeout: 120s
  - Temperature: 0.6

### 4. Configuration (`app/core/config.py`)

**Environment Variables:**
- `LM_STUDIO_URL` - LLM endpoint
- `MODEL_NAME` - Model selection
- `LLM_TIMEOUT` - Request timeout
- `LLM_TEMPERATURE` - Sampling temperature
- `AGENT_NAME` - Agent display name
- `CONTEXT_WINDOW` - Max history messages
- `MAX_TOOL_LOOPS` - Safety limit for tool chains
- `WEB_HOST` / `WEB_PORT` - Server binding

**Google API Scopes:**
- Gmail: `https://www.googleapis.com/auth/gmail.modify`
- Docs: `https://www.googleapis.com/auth/documents`, `https://www.googleapis.com/auth/drive`
- Sheets: `https://www.googleapis.com/auth/spreadsheets`

---

## Integration Modules

### Gmail MCP (`app/integrations/gmail/`)

**140+ Total Tools:**

| Category | Count | Examples |
|----------|-------|----------|
| **Core V1** | 40+ | `get_emails`, `send_email`, `draft_email`, `search_emails` |
| **Core V2** | 20+ | `email_action`, `email_modify`, `resolve_email_id`, `batch_email_action` |
| **AI Tools** | 20+ | `summarize_email`, `classify_email`, `detect_urgency`, `draft_reply` |
| **Batch Ops** | 10+ | `archive_emails`, `trash_emails`, `mark_emails_read` |

**Key Features:**
- Name→ID resolution (use "latest email" instead of cryptic ID)
- Batch operations for bulk actions
- AI-powered email analysis and drafting
- Context-aware pronoun resolution ("it", "this", "that", "first email")

**Recent Fixes:**
- AI tools now accept `message_id` and fetch content internally
- `trash_email` / `delete_email` return standardized responses
- Compound commands work via multi-pass LLM execution
- Date range queries properly inclusive for same-day searches

### Drive MCP (`app/integrations/drive/`)

**30+ Tools:**

| Category | Examples |
|----------|----------|
| **ID Resolution** | `resolve_file_id`, `resolve_folder_id` |
| **Browse** | `list_files`, `list_folders`, `get_folder_contents` (paginated) |
| **Search** | `search_files`, `search_files_by_type` |
| **Organize** | `move_file`, `copy_file`, `rename_file` |
| **Batch Ops** | `delete_files`, `move_files`, `copy_files` |
| **Sharing** | `share_file`, `share_file_publicly`, `get_shareable_link` |
| **Upload/Download** | `upload_file`, `download_file` |

**Key Features:**
- Accept names OR IDs for all operations
- Pagination support for large result sets
- TTL-based caching (5min default)
- Safety layer blocks dangerous sharing operations (no ownership transfer)
- Duplicate file detection

### Docs MCP (`app/integrations/docs/`)

**30+ Tools:**

| Category | Examples |
|----------|----------|
| **V2 Unified** | `doc_action`, `doc_modify`, `resolve_doc_id` |
| **Read** | `read_document`, `list_documents`, `search_documents` |
| **Write** | `append_content`, `insert_content`, `replace_section`, `delete_section` |
| **Analysis** | `analyze_summary`, `analyze_structure`, `analyze_key_points` |
| **Batch** | `append_multiple_sections`, `replace_multiple_sections` |

### Sheets MCP (`app/integrations/sheets/`)

**30+ Tools:**

| Category | Examples |
|----------|----------|
| **V2 Unified** | `sheet_action`, `sheet_modify`, `resolve_sheet_id` |
| **Read/Write** | `read_sheet_data`, `write_range`, `append_rows` |
| **Batch** | `update_multiple_ranges`, `append_rows_bulk`, `delete_rows_bulk` |
| **Formulas** | `insert_formula`, `detect_formula_columns` |
| **Tabs** | `add_tab`, `rename_tab`, `delete_tab` |

---

## Web Interface (`app/ui/web.py`)

**Features:**
- REST API for chat and file operations
- File upload support (text extraction, image vision)
- Mode selection (Gmail, Drive, Docs, Sheets, Unified)
- Inbox overview sidebar
- Real-time email viewer modal

**API Endpoints:**
- `GET /api/health` - Health check
- `GET /api/inbox` - Fetch recent emails
- `GET /api/email?message_id=...` - Get specific email
- `POST /api/chat` - Send message to agent
- `POST /api/reset_history` - Reset conversation context

**File Upload Support:**
- Text files (`.txt`, `.md`, `.csv`, `.json`, `.py`, etc.)
- Word documents (`.docx`) - paragraph + table text extraction
- PDFs (`.pdf`) - PyPDF2 text extraction
- Images (`.png`, `.jpg`, `.gif`, `.webp`) - vision model processing

---

## Agent Orchestration Flow

```
User Input
    ↓
┌─────────────────┐
│ Intent Detect   │──→ Strict regex shortcuts (date queries, simple commands)
│ (Strict Path)   │    No pronouns, explicit IDs only
└─────────────────┘
    ↓ (if no match)
┌─────────────────┐
│ LLM Resolution  │──→ Build context-aware prompt
│                 │    Call LM Studio
│                 │    Parse JSON/proprietary response
└─────────────────┘
    ↓
┌─────────────────┐
│ Tool Execution  │──→ Execute via MCP server
│                 │    Update context from result
└─────────────────┘
    ↓
┌─────────────────┐
│ Format Response │──→ HTML formatting for UI
└─────────────────┘
    ↓
Response to User
```

### Compound Commands Flow

```
"Show unread and mark first as read"
    ↓
Split: "Show unread" + "mark first as read"
    ↓
Step 1: LLM call → execute `get_unread_emails`
        → Update context with email IDs
    ↓
Build summary with IDs for Step 2
    ↓
Step 2: LLM call with summary → execute `mark_as_read`
    ↓
Combined response
```

---

## System Prompts

**Gmail Mode System Prompt Includes:**
- Available tools reference (50+ tools with descriptions)
- **Context Resolution Rules:**
  - Positional mapping: "1st email" → first ID in context
  - Pronoun mapping: "this", "it", "that" → current entity
- **Compound Command Instructions:** LLM returns ONE tool call per step
- **Response Format:** Strict JSON with `requires_tool`, `tool`, `arguments`

---

## Authentication

**OAuth2 Flow:**
1. `credentials.json` - OAuth2 client credentials
2. `token.json` - Cached user access/refresh tokens
3. Scopes configured per integration in `config.py`

---

## Testing

**Test Files:**
- `gmail_test_questions.md` - 285 test scenarios organized by category
- `test_gmail_tools_real_data.py` - 75K lines of Gmail integration tests
- `test_drive_tools_real_data.py` - 44K lines of Drive tests
- `test_docs_tools_real_data.py` - 35K lines of Docs tests
- `test_sheets_tools_real_data.py` - 35K lines of Sheets tests

---

## Key Technical Decisions

1. **Local LLM First:** Uses LM Studio for privacy; no cloud API calls
2. **Dual API Versions:** V1 for compatibility, V2 for new agent-native features
3. **Context-Rich LLM Prompts:** Injects entity history to resolve pronouns
4. **Strict Path + LLM Hybrid:** Fast regex for common cases, LLM for complex
5. **Standardized Responses:** All tools return `{"success": bool, ...}` format
6. **Multi-Pass Compound Commands:** Two LLM calls with context refresh between

---

## Known Issues & Recent Fixes

| Issue | Status | Fix |
|-------|--------|-----|
| AI tools `message_id` error | ✅ Fixed | Tools now fetch email content internally |
| Compound commands not working | ✅ Fixed | Multi-pass LLM execution implemented |
| Trash email not moving to trash | ✅ Fixed | Standardized response handling |
| Date range same-day queries | ✅ Fixed | Added +1 day for inclusive search |
| LLM tool call token leaks | ✅ Fixed | Sanitization function strips tokens |

---

## Development Commands

```bash
# Install dependencies
pip install -e .

# Run web interface (default)
python main.py

# Run CLI mode
python main.py --cli

# Run desktop mode
python main.py --desktop

# Run tests
python test_gmail_tools_real_data.py
```

---

## Architecture Principles

1. **Agent-Native Design:** Tools accept natural references ("latest email") not just IDs
2. **Context Awareness:** Full conversation history and entity tracking
3. **Fail-Safe:** Structured error responses, never crash the agent loop
4. **Extensible:** Registry pattern allows easy addition of new tools
5. **Backward Compatible:** V1 APIs continue working alongside V2

---

**Total Lines of Code:** ~150,000+ (including tests)  
**Core Implementation:** ~20,000 lines  
**Test Coverage:** 4 comprehensive test suites with real data
