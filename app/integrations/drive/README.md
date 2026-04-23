# Google Drive MCP v2 — Agent-Native Integration

> **Production-ready, agent-optimized Google Drive integration with 30+ tools**

## 🎯 What's New in v2

| Feature | v1 | v2 |
|---------|----|----|
| **Name→ID Resolution** | ❌ Manual IDs only | ✅ Accept names or IDs |
| **Pagination** | ❌ Limited results | ✅ Full pagination support |
| **Batch Operations** | ❌ Single file only | ✅ Bulk delete/move/copy |
| **Advanced Search** | ❌ Simple text search | ✅ Multi-filter search |
| **Error Handling** | ❌ Generic exceptions | ✅ Structured error classes |
| **Caching** | ❌ None | ✅ TTL-based caching |
| **Logging** | ❌ Basic | ✅ Structured observability |
| **Permission Safety** | ❌ All roles allowed | ✅ Dangerous roles blocked |
| **MCP Suggestions** | ❌ None | ✅ File-type intelligence |
| **Download Support** | ❌ None | ✅ File download/export |

---

## 📦 Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Drive MCP v2                          │
├─────────────────────────────────────────────────────────┤
│  registry.py  →  Tool registration (30+ tools)            │
│  core_v2.py   →  Full-featured implementation           │
│  core.py      →  Backward-compatible wrapper            │
│  utils.py     →  Errors, cache, logging helpers          │
└─────────────────────────────────────────────────────────┘
```

---

## 🔧 Tools Reference

### ID Resolution (Agent-Friendly)
```python
# Resolve names to IDs automatically
resolve_file_id(name_or_id: str, allow_ambiguity: bool = False) -> str
resolve_folder_id(name_or_id: str, allow_ambiguity: bool = False) -> str
```

**Example:**
```python
# Can use name instead of cryptic ID
move_file(file_name="Report Q3", destination_folder_name="Archives")
```

---

### Browse (with Pagination)
```python
list_files(limit=10, folder_id="", page_token="")
list_folders(limit=20, page_token="")
get_folder_contents(folder_id, limit=20, page_token="")
get_starred_files(limit=20, page_token="")
get_recent_files(limit=10)
```

**Response Format:**
```json
{
  "files": [...],
  "next_page_token": "...",
  "total_count": 50,
  "has_more": true
}
```

---

### Advanced Search
```python
search_files(
    query="budget",
    file_type="sheet",           # doc, sheet, pdf, image, etc.
    date_from="2024-01-01",
    date_to="2024-12-31",
    owner="user@example.com",
    folder_id="...",
    starred_only=False,
    limit=10,
    page_token=""
)
```

---

### Metadata & Intelligence
```python
get_file_metadata(file_id_or_name)  # Returns with suggested_mcp field
get_storage_info()                 # Usage statistics
```

**Metadata Response:**
```json
{
  "id": "...",
  "name": "Project Plan",
  "type": "Google Sheet",
  "mime": "application/vnd.google-apps.spreadsheet",
  "url": "https://docs.google.com/...",
  "parent": "folder_id",
  "modified_time": "2024-01-15T10:30:00",
  "suggested_mcp": "sheets",    // <-- AI hint!
  "is_google_workspace_file": true
}
```

---

### Batch Operations (New)
```python
delete_files(file_ids=["..."], file_names=["..."])
move_files(file_ids=["..."], destination_folder_id="...")
copy_files(file_ids=["..."], destination_folder_id="...")
```

**Example:**
```python
delete_files(file_names=["Temp1.txt", "Temp2.txt", "Old Report.pdf"])
```

---

### Sharing (with Safety Layer)
```python
share_file(file_id, email, role="reader")  # Blocks 'owner' role
share_file_publicly(file_id, role="reader")
get_shareable_link(file_id)
remove_permission(file_id, permission_id)
remove_access(file_id, email)
make_file_private(file_id)
get_file_permissions(file_id)
```

**Safety Features:**
- ❌ Ownership transfer blocked (`role="owner"` rejected)
- ❌ Sharing with self blocked
- ✅ Email validation before sharing

---

### Upload & Download
```python
upload_file(file_path, folder_id="", folder_name="", file_name="")
download_file(file_id, download_path="", export_format="pdf")
```

---

### Utility Tools
```python
find_duplicates(folder_id="", checksum=False)  # Detect duplicate files
get_drive_stats()                              # MCP usage statistics
clear_drive_cache()                            # Clear cache manually
```

---

## 🛡️ Error Handling

```python
from app.integrations.drive.utils import (
    DriveError,              # Base exception
    DriveNotFoundError,      # File/folder not found
    DrivePermissionError,    # Access denied
    DriveRateLimitError,     # API quota exceeded
    DriveValidationError,    # Invalid input
    DriveAmbiguityError,     # Multiple name matches
)

# Example usage
try:
    file_id = resolve_file_id("Budget")
except DriveAmbiguityError as e:
    # Multiple files named "Budget"
    print(f"Did you mean: {e.details['matches']}")
except DriveNotFoundError:
    print("File not found")
```

---

## 💾 Caching

Automatic TTL-based caching for performance:

```python
from app.integrations.drive.utils import _drive_cache, invalidate_cache

# Cache stats
print(_drive_cache.stats())  # {'size': 10, 'hits': 50, 'misses': 5, 'hit_rate': '90.9%'}

# Manual invalidation
invalidate_cache("search")   # Clear search-related cache
invalidate_cache()           # Clear all cache
```

---

## 📊 Observability

Structured logging for every tool call:

```python
from app.integrations.drive.utils import _drive_logger

# Get recent calls
print(_drive_logger.get_recent_calls(5))

# Get statistics
print(_drive_logger.get_stats())
# {'total_calls': 150, 'success_rate': '98.7%', 'avg_latency_ms': 245.3}
```

---

## 🔀 Backward Compatibility

All existing code continues to work:

```python
# v1 code (still works)
from app.integrations.drive import core
core.list_files(limit=10)  # Returns list, not paginated response

# v2 code (new features)
from app.integrations.drive import core_v2
core_v2.list_files(limit=10, page_token="...")  # Returns paginated response
```

---

## 📋 Migration Guide

### From v1 to v2

| v1 (Old) | v2 (New) |
|----------|----------|
| `core.list_files()` | `core_v2.list_files()` (paginated) |
| `core.get_file_metadata(id)` | `core_v2.get_file_metadata(id)` (with `suggested_mcp`) |
| `core.share_file(id, email, role="owner")` | Blocked! Use web UI for ownership transfer |
| N/A | `core_v2.resolve_file_id("name")` |
| N/A | `core_v2.delete_files(file_names=["a", "b"])` |
| N/A | `core_v2.search_files(query="x", file_type="pdf")` |
| N/A | `core_v2.download_file(id)` |

---

## 🔐 Security Best Practices

1. **Never allow ownership transfer via API** - Use web UI
2. **Validate emails before sharing** - Automatic in v2
3. **Cache sensitive results minimally** - Default 5min TTL
4. **Log tool calls without sensitive data** - Automatic sanitization
5. **Sanitize user input** - Automatic query escaping

---

## 🚀 Quick Start

```python
from app.integrations.drive import core_v2 as drive

# 1. List files with pagination
result = drive.list_files(limit=20)
print(f"Found {result['total_count']} files")
if result['has_more']:
    more = drive.list_files(limit=20, page_token=result['next_page_token'])

# 2. Search with filters
sheets = drive.search_files(
    query="budget",
    file_type="sheet",
    date_from="2024-01-01"
)

# 3. Use names instead of IDs
drive.move_file(
    file_name="Report.pdf",
    destination_folder_name="Q4 Archive"
)

# 4. Batch operations
drive.delete_files(file_names=["temp1.txt", "temp2.txt"])

# 5. Safe sharing
drive.share_file(file_id="...", email="colleague@corp.com", role="writer")
# Fails: drive.share_file(..., role="owner")  # Blocked!

# 6. Get MCP suggestions
meta = drive.get_file_metadata("Project Plan")
if meta['suggested_mcp'] == 'sheets':
    # Switch to Sheets MCP for this file
    pass
```

---

## 📊 Performance

| Operation | v1 | v2 |
|-----------|----|----|
| List 100 files | ~3s | ~1.5s (cached) |
| Metadata lookup | ~800ms | ~100ms (cached) |
| Name resolution | N/A | ~400ms + search |
| Batch delete 10 files | 10 calls | 1 orchestrated call |

---

## 🧪 Testing

```bash
# Run Drive MCP tests
python -m pytest test_drive_tools_real_data.py -v

# Test specific features
python -c "from app.integrations.drive import core_v2; print(core_v2.get_drive_stats())"
```

---

## 📚 See Also

- `core_v2.py` - Full implementation (~900 lines)
- `utils.py` - Errors, cache, logging (~400 lines)
- `core.py` - Backward-compatible wrapper (~350 lines)
- `registry.py` - Tool definitions (30+ tools)

---

**Version:** 2.0.0  
**Last Updated:** 2024  
**Maintainer:** AI Agent Systems Team
