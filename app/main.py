import sys
import threading
import time
from app.core.mcp import MCPServer
from app.tools.calculator import evaluate_expression as calculator
from app.tools.filesystem import read_file, list_files
from app.integrations.gmail.registry import GMAIL_TOOLS
from app.integrations.docs.registry import DOCS_TOOLS
from app.ui.web import run_web
from app.ui.desktop import run_desktop

_scheduler_started = False
_scheduler_lock = threading.Lock()

def _ensure_scheduler_running():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        def poll():
            while True:
                time.sleep(30)
        t = threading.Thread(target=poll, daemon=True)
        t.start()
        _scheduler_started = True

_cached_mcp = None
def build_server():
    global _cached_mcp
    if _cached_mcp:
        return _cached_mcp
    mcp = MCPServer()
    mcp.register_tool("calculator", calculator)
    mcp.register_tool("read_file", read_file)
    mcp.register_tool("list_files", list_files)
    for name, func in GMAIL_TOOLS.items():
        mcp.register_tool(name, func)
    for name, func in DOCS_TOOLS.items():
        mcp.register_tool(name, func)
    _ensure_scheduler_running()
    _cached_mcp = mcp
    return mcp

def run_cli():
    print("CLI mode")

def main():
    if "--cli" in sys.argv: run_cli()
    elif "--desktop" in sys.argv: run_desktop()
    else: run_web()

if __name__ == "__main__":
    main()
