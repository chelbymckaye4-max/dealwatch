#!/usr/bin/env python3
"""
One-time helper: get a READ-ONLY Gmail refresh token so Dealwatch can read store sale / price-drop emails.

Before running (about 10 minutes, use a personal Google account; work accounts often block this):
  1. console.cloud.google.com > create a project > APIs & Services > Library > enable "Gmail API".
  2. OAuth consent screen: User type External. Add yourself as a test user.
     IMPORTANT: while an app is in "Testing", Google expires its refresh token after 7 days.
     For a lasting token click "Publish app" (it will show an "unverified app" warning that you can accept for your own use).
  3. Credentials > Create credentials > OAuth client ID > type "Desktop app". Copy the client ID and secret.

Run:   python tools/gmail_auth.py
Then add these three GitHub Actions secrets: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN.

Dealwatch only asks for read-only access, only looks at senders listed in GMAIL_QUERY (in dealwatch.py), and keeps nothing
except the deal alerts it creates.
"""
import http.server, os, urllib.parse, webbrowser
import requests

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
PORT = 8765

def main():
    cid = os.environ.get("GMAIL_CLIENT_ID") or input("OAuth client ID: ").strip()
    secret = os.environ.get("GMAIL_CLIENT_SECRET") or input("OAuth client secret: ").strip()
    redirect = f"http://localhost:{PORT}"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent"})
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (q.get("code") or [""])[0]
            result["error"] = (q.get("error") or [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Done. You can close this tab and go back to the terminal.</h3>")
        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print("Opening your browser. If it doesn't open, visit:\n" + url + "\n")
    webbrowser.open(url)
    server.handle_request()
    if not result.get("code"):
        raise SystemExit("No authorization code received: " + (result.get("error") or "unknown error"))
    r = requests.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": result["code"], "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    j = r.json()
    if "refresh_token" not in j:
        raise SystemExit("Google did not return a refresh token: " + str(j))
    print("\nAdd these as GitHub Actions secrets:\n")
    print("GMAIL_CLIENT_ID     =", cid)
    print("GMAIL_CLIENT_SECRET =", secret)
    print("GMAIL_REFRESH_TOKEN =", j["refresh_token"])

if __name__ == "__main__":
    main()
