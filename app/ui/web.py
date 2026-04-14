import os
import json
import urllib.parse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from app.core.config import WEB_HOST, WEB_PORT, PROJECT_ROOT
from app.integrations.gmail.core import get_emails, get_email_by_id
from app.core.orchestrator import run_agent


STATIC_DIR = PROJECT_ROOT / "app" / "ui" / "static"

agent_state = {}

class AgentHTTPRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/api/health':
            self.send_json({"status": "ok"})
        elif parsed_path.path == '/api/inbox':
            try:
                emails = get_emails(limit=20)
                self.send_json({"emails": emails})
            except PermissionError as e:
                self.send_error(401, str(e))
            except Exception as e:
                self.send_error(500, str(e))
        elif parsed_path.path == '/api/email':
            query = urllib.parse.parse_qs(parsed_path.query)
            msg_id = query.get('message_id', [''])[0]
            if msg_id:
                sel_email = get_email_by_id(msg_id)
                self.send_json({"email": sel_email})
            else:
                self.send_error(400, "Missing message_id")
        else:
            super().do_GET()

    def do_POST(self):
        global agent_state
        parsed_path = urllib.parse.urlparse(self.path)
        if parsed_path.path == '/api/chat':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length == 0:
                    self.send_error(400, "Empty request body")
                    return
                body = self.rfile.read(length)
                data = json.loads(body.decode('utf-8'))
                msg = data.get('message', '')
                attachment = data.get("attachment")
                
                if attachment:
                    import base64
                    from pathlib import Path
                    from app.core.config import DATA_DIR
                    ups = DATA_DIR / "uploads"
                    ups.mkdir(exist_ok=True)
                    fpath = ups / attachment["name"]
                    fpath.write_bytes(base64.b64decode(attachment["data"]))
                    agent_state["last_attachment_path"] = str(fpath)
                    msg += f"\n[System: Attached file {attachment['name']}]"
                
                from app.main import build_server
                mcp = build_server()
                reply = run_agent(msg, mcp, agent_state)
                self.send_json({"reply": reply})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json({"reply": f"<b>System Error:</b> {str(e)}"})
        else:
            self.send_error(404, "Not Found")

    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

def run_web():
    server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), AgentHTTPRequestHandler)
    print(f"Starting web server on http://{WEB_HOST}:{WEB_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
        server.server_close()
