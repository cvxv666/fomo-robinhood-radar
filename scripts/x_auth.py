"""Authorise the radar's X app for the bot's account, once, on this machine.

    set X_CLIENT_ID=...            (from the developer portal; the app's OAuth 2.0 client id)
    set X_CLIENT_SECRET=...        (only if the app is a confidential client)
    .venv/Scripts/python scripts/x_auth.py

It prints one URL. Open it in a browser where you are signed in as the BOT account (not your
main), approve, and the browser lands on http://127.0.0.1:8765/callback - this script is
listening there, takes the code, trades it for tokens and writes them to X_TOKEN_FILE (default
x_token.json beside the repo). Then copy that file to the server as /opt/fomoradar/x_token.json,
owned by radar, mode 600. The redirect URL above must be registered in the app's settings.
Nothing secret is printed.
"""
import http.server
import os
import pathlib
import secrets
import sys
import threading
import urllib.parse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from fomo_agent.sources.x import XClient, authorize_url, pkce_pair  # noqa: E402

REDIRECT = os.environ.get("X_REDIRECT_URI", "http://127.0.0.1:8765/callback")
client_id = os.environ.get("X_CLIENT_ID", "").strip()
if not client_id:
    sys.exit("set X_CLIENT_ID first (the app's OAuth 2.0 client id from the developer portal)")
token_file = os.environ.get("X_TOKEN_FILE", "x_token.json")
verifier, challenge = pkce_pair()
state = secrets.token_urlsafe(16)
got: dict = {}


class Catch(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] != state:
            self.send_response(400); self.end_headers(); self.wfile.write(b"state mismatch"); return
        got["code"] = q.get("code", [""])[0]
        self.send_response(200); self.end_headers()
        self.wfile.write(b"Authorised. You can close this tab; the token is being written.")

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 8765), Catch)
threading.Thread(target=srv.handle_request, daemon=True).start()
print("\nOpen this in the browser where you are signed in as the bot account, and approve:\n")
print(authorize_url(client_id, REDIRECT, state, challenge))
print("\nWaiting for the browser to come back to", REDIRECT, "...")
while "code" not in got:
    threading.Event().wait(0.5)
client = XClient(client_id=client_id, client_secret=os.environ.get("X_CLIENT_SECRET", ""), token_file=token_file)
tokens = client.exchange_code(got["code"], verifier, REDIRECT)
print(f"\nDone: tokens written to {token_file} (scope: {tokens.get('scope', '?')}). Copy it to the server as /opt/fomoradar/x_token.json.")
