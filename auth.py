"""Run `python auth.py` to authenticate Gmail before starting the app."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.config import CREDENTIALS_FILE, GMAIL_SCOPES as SCOPES, TOKEN_FILE
from google_auth_oauthlib.flow import InstalledAppFlow

if __name__ == "__main__":
    print(f"Loading credentials from {CREDENTIALS_FILE}")
    if not CREDENTIALS_FILE.exists():
        print(f"Error: Credentials file not found at {CREDENTIALS_FILE}")
        print("Please download credentials.json from Google Cloud Console and place it in the project root.")
        sys.exit(1)
        
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"Token saved to {TOKEN_FILE}. Now run: python main.py")
