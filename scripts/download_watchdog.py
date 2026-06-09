#!/usr/bin/env python3
"""
Download Watchdog for Media Requests Telegram Bot.

Strategy: Watch the Radarr/Sonarr queue. Items appear while downloading and
disappear only AFTER the arr service has fully imported the file. When a
tracked item vanishes from the queue, that's the signal to notify and refresh Plex.

No premature triggers — we only act on confirmed imports.
State file tracks queue item IDs we've seen so we can detect disappearances.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

# --- Config ---
PROFILE_DIR = Path.home() / ".hermes" / "profiles" / "media-requests"
STATE_FILE = PROFILE_DIR / "state" / "watchdog_state.json"
ENV_FILE = PROFILE_DIR / ".env"

# How long to keep track of completed IDs (prevents re-notification on state loss)
MAX_AGE_HOURS = 24

def load_env():
    if not ENV_FILE.exists():
        print(f"ERROR: .env not found at {ENV_FILE}", file=sys.stderr)
        sys.exit(1)
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except:
            return {"known_queue_ids": {}, "notified_ids": [], "last_check": None}
    return {"known_queue_ids": {}, "notified_ids": [], "last_check": None}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state["last_check"] = datetime.now(timezone.utc).isoformat()

    # Prune old notified IDs to prevent unbounded growth
    if len(state.get("notified_ids", [])) > 500:
        state["notified_ids"] = state["notified_ids"][-500:]

    STATE_FILE.write_text(json.dumps(state, indent=2))

def api_get(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MediaWatchdog/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"API GET failed: {url} -> {e}", file=sys.stderr)
        return None

def api_post(url, data=None, timeout=15):
    try:
        body = json.dumps(data).encode() if data else b""
        req = urllib.request.Request(url, data=body, method="POST",
                                      headers={"Content-Type": "application/json",
                                               "User-Agent": "MediaWatchdog/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"API POST failed: {url} -> {e}", file=sys.stderr)
        return None

def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    for attempt in range(3):
        result = api_post(url, data)
        if result and result.get("ok"):
            print(f"Telegram: sent message ({len(text)} chars)")
            return True
        elif result and result.get("error_code") == 429:
            retry_after = result.get("parameters", {}).get("retry_after", 5)
            print(f"Telegram rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
        else:
            print(f"Telegram FAILED: {result}", file=sys.stderr)
            return False
    return False

def check_arr_queue(env):
    """Get current items in Radarr/Sonarr queues. Returns dict of {queue_id: title}."""
    current_queue = {}

    for service, key, url_key in [("Radarr", "RADARR_API_KEY", "RADARR_URL"),
                                   ("Sonarr", "SONARR_API_KEY", "SONARR_URL")]:
        url = f"{env[url_key]}/api/v3/queue?apikey={env[key]}&pageSize=100&includeUnknownMovieItems=true"
        data = api_get(url)
        if not data:
            continue

        for record in data.get("records", []):
            queue_id = str(record.get("id", ""))
            if not queue_id:
                continue

            # Build a readable title
            title = record.get("title", "Unknown")
            if service == "Radarr" and record.get("movie"):
                m = record["movie"]
                title = m.get("title", title)
                year = m.get("year", "")
                if year:
                    title = f"{title} ({year})"
            elif service == "Sonarr" and record.get("series"):
                series_title = record["series"].get("title", title)
                ep = record.get("episode", {})
                season = ep.get("seasonNumber", "?")
                episode = ep.get("episodeNumber", "?")
                title = f"{series_title} S{season:02d}E{episode:02d}"

            status = record.get("status", "Unknown")
            size_mb = record.get("size", 0) / (1024 * 1024)

            current_queue[f"{service}_{queue_id}"] = {
                "title": title,
                "service": service,
                "status": status,
                "size_mb": round(size_mb, 1),
            }

    return current_queue

def trigger_plex_refresh(env):
    """Trigger Plex library scan."""
    plex_url = env.get("PLEX_URL", "http://10.0.60.88:32400")
    plex_token = env.get("PLEX_TOKEN", "")

    if not plex_token:
        try:
            vault_token = Path.home().joinpath(".vault-token").read_text().strip()
            url = "http://10.0.0.20:8200/v1/secret/data/media/plex"
            req = urllib.request.Request(url, headers={"X-Vault-Token": vault_token})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                plex_token = data.get("data", {}).get("data", {}).get("token", "")
        except Exception as e:
            print(f"Could not fetch Plex token from vault: {e}", file=sys.stderr)

    if not plex_token:
        print("WARNING: No Plex token available, skipping refresh", file=sys.stderr)
        return False

    url = f"{plex_url}/library/sections/all/refresh?X-Plex-Token={plex_token}"
    try:
        req = urllib.request.Request(url, method="POST",
                                      headers={"User-Agent": "MediaWatchdog/2.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"Plex refresh triggered: {resp.status}")
            return True
    except Exception as e:
        print(f"Plex refresh failed: {e}", file=sys.stderr)
        return False

def main():
    env = load_env()
    state = load_state()

    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("MEDIA_CHAT_ID", "")

    if not bot_token or not chat_id:
        print("ERROR: Missing TELEGRAM_BOT_TOKEN or MEDIA_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    # Get what's currently in the arr queue
    current_queue = check_arr_queue(env)
    current_ids = set(current_queue.keys())
    known_ids = set(state.get("known_queue_ids", {}).keys())
    notified_ids = set(state.get("notified_ids", []))

    # Items that WERE in the queue but are NOW gone = finished importing
    disappeared = known_ids - current_ids

    notifications = []
    new_notified = list(state.get("notified_ids", []))

    for item_id in disappeared:
        # Skip if we already notified about this one
        if item_id in notified_ids:
            continue

        info = state["known_queue_ids"].get(item_id, {})
        title = info.get("title", "Unknown")
        size_mb = info.get("size_mb", 0)
        service = info.get("service", "Unknown")

        if size_mb > 0:
            notifications.append(f"Download complete: {title} ({size_mb} MB)")
        else:
            notifications.append(f"Download complete: {title}")

        new_notified.append(item_id)

    # Refresh Plex only if there are new completions
    if notifications:
        trigger_plex_refresh(env)

    # Send notifications
    for msg in notifications:
        send_telegram(bot_token, chat_id, msg)
        time.sleep(0.5)

    # Save state: current queue becomes the new known queue
    new_state = {
        "known_queue_ids": current_queue,
        "notified_ids": new_notified,
    }
    save_state(new_state)

    if notifications:
        print(f"Sent {len(notifications)} notification(s), {len(current_ids)} items in queue")
    else:
        print(f"No new completions. {len(current_ids)} items in queue, {len(disappeared)} disappeared (already notified)")

if __name__ == "__main__":
    main()
