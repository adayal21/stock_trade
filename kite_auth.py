"""
kite_auth.py — Daily Zerodha login for the India bot.
======================================================
Run this ONCE every morning before market open (before 9:15 AM IST).
It opens a browser login URL, you log in, paste the redirect URL back,
and it writes KITE_ACCESS_TOKEN to your .env file automatically.

The access token is valid until 6 AM the next day (regulatory requirement).

Usage:
    python kite_auth.py

Takes about 30 seconds. After this, main.py runs unattended all day.

Requirements:
    pip install kiteconnect python-dotenv
"""

import os
import sys
import hashlib
import webbrowser
from urllib.parse import urlparse, parse_qs
from pathlib import Path

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: pip install kiteconnect")
    sys.exit(1)

try:
    from dotenv import load_dotenv, set_key
except ImportError:
    print("ERROR: pip install python-dotenv")
    sys.exit(1)

ENV_FILE = Path(__file__).parent / ".env"

def main():
    # Load existing .env
    load_dotenv(ENV_FILE)

    api_key    = os.environ.get("KITE_API_KEY", "").strip()
    api_secret = os.environ.get("KITE_API_SECRET", "").strip()

    if not api_key or not api_secret:
        print("ERROR: KITE_API_KEY and KITE_API_SECRET not set in .env")
        print(f"Edit {ENV_FILE} and fill in your credentials.")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)

    # Step 1 — Generate and open login URL
    login_url = kite.login_url()
    print("=" * 60)
    print("  Zerodha Kite — Daily Login")
    print("=" * 60)
    print()
    print("Step 1: Opening login page in your browser...")
    print(f"  URL: {login_url}")
    print()

    try:
        webbrowser.open(login_url)
    except Exception:
        print("  (Could not auto-open browser — copy the URL above manually)")

    print("Step 2: Log in with your Zerodha credentials.")
    print("        After login you'll be redirected to your redirect URL.")
    print()
    print("Step 3: Copy the FULL redirect URL from your browser address bar")
    print("        It looks like: https://127.0.0.1?request_token=XXXX&action=login&status=success")
    print()

    redirect_url = input("Paste the full redirect URL here: ").strip()

    # Step 2 — Extract request_token from URL
    try:
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        request_token = params.get("request_token", [None])[0]
        if not request_token:
            raise ValueError("request_token not found in URL")
    except Exception as e:
        print(f"\nERROR: Could not parse request_token from URL: {e}")
        print("Make sure you copied the full redirect URL including query params.")
        sys.exit(1)

    print(f"\n  request_token extracted: {request_token[:10]}...")

    # Step 3 — Exchange for access_token
    try:
        session = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session["access_token"]
        user_id      = session.get("user_id", "")
        user_name    = session.get("user_name", "")
        login_time   = session.get("login_time", "")
    except Exception as e:
        print(f"\nERROR: Token exchange failed: {e}")
        print("The request_token may have expired (valid only for a few minutes).")
        print("Run kite_auth.py again and paste the URL quickly after login.")
        sys.exit(1)

    # Step 4 — Write access_token to .env
    set_key(str(ENV_FILE), "KITE_ACCESS_TOKEN", access_token)

    print()
    print("=" * 60)
    print("  Login successful!")
    print("=" * 60)
    print(f"  User      : {user_name} ({user_id})")
    print(f"  Login time: {login_time}")
    print(f"  Token     : {access_token[:10]}...{access_token[-4:]}")
    print(f"  Written to: {ENV_FILE}")
    print()
    print("  Access token is valid until 6 AM tomorrow.")
    print("  You can now run: python main.py")
    print("=" * 60)


if __name__ == "__main__":
    main()