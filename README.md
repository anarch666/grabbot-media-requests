# @grabbot — Media Requests Telegram Bot

A Telegram bot that lets group members request movies and TV shows for download. Built as a [Hermes Agent](https://hermes-agent.nousresearch.com) profile — runs as its own isolated instance with a dedicated systemd service.

## How It Works

```
User in Telegram group: "get The Matrix"
  → @grabbot searches Seerr
  → Submits request to Seerr
  → Seerr tells Radarr/Sonarr to grab it
  → SABnzbd downloads it
  → Watchdog detects import completion
  → Plex library refresh triggered
  → Group notified: "Download complete: The Matrix (1999)"
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Telegram Group "Media" (-5200914292)               │
│  @grabbot (mention required)                        │
└──────────────┬──────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────┐
│  Hermes Profile: media-requests                     │
│  ├── config.yaml (model, telegram, security)        │
│  ├── SOUL.md (bot persona + safety rules)           │
│  ├── .env (secrets)                                 │
│  └── skills/media-request-flow/ (API references)    │
└──────────────┬──────────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────────┐
│  LiteLLM Proxy (CT 220)                             │
│  Model: deepseek-v4-flash                           │
└─────────────────────────────────────────────────────┘
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌────────┐
│ Seerr  │ │Radarr/ │ │TorBox  │
│ :5055  │ │Sonarr  │ │(fallback)
└────────┘ │:7878/  │ └────────┘
           │ :8989  │
           └───┬────┘
               ▼
         ┌──────────┐      ┌──────────┐
         │ SABnzbd  │─────▶│   Plex   │
         │  :8088   │      │  :32400  │
         └──────────┘      └──────────┘
               ▲
               │
    ┌──────────┴──────────┐
    │  Download Watchdog   │
    │  (cron, every 3min)  │
    │  watches arr queue   │
    └─────────────────────┘
```

## Components

### 1. Hermes Profile (`media-requests`)

An isolated Hermes instance with its own config, skills, memory, and sessions. Separate from the main hermes instance.

### 2. SOUL.md — Bot Persona & Safety Rules

The bot's brain. Defines:
- **ONLY** handles media requests — refuses everything else
- Workflow: Search Seerr → Request → Confirm → Done
- Never reveals API keys, IPs, or infrastructure details
- Never inspects Radarr/Sonarr internals
- Plain text responses only (no markdown)
- Falls back to Radarr/Sonarr direct only if Seerr fails
- Falls back to TorBox only if arr stack has no results

### 3. Download Watchdog (`download_watchdog.py`)

A cron job (every 3 minutes) that:
- Polls Radarr/Sonarr queue API
- Tracks items currently in the queue
- When an item **disappears** from the queue = import complete
- Triggers Plex library refresh
- Sends Telegram notification to the group
- Only acts on confirmed imports — no premature triggers

**Why queue-based instead of SAB history?** SAB marks items "Completed" when the download finishes, but Radarr/Sonarr still needs to import the file. The arr queue item only disappears after the import is truly done.

### 4. Systemd Service (`hermes-media-bot.service`)

Runs the Hermes gateway for the media-requests profile. Auto-restarts on failure.

## Setup (from scratch)

### Prerequisites

- Hermes Agent installed at `~/.hermes/hermes-agent/`
- LiteLLM proxy running with a model configured
- Media stack: Seerr, Radarr, Sonarr, SABnzbd, Plex
- Telegram bot token (create via [@BotFather](https://t.me/BotFather))

### Step 1: Create the Hermes profile

```bash
hermes profile create media-requests
```

### Step 2: Create `.env` file

Copy `.env.example` to `~/.hermes/profiles/media-requests/.env` and fill in real values. All API keys, bot token, chat ID, and service URLs go here.

### Step 3: Create `config.yaml`

Copy `config.yaml` to `~/.hermes/profiles/media-requests/config.yaml`. Update the LiteLLM base_url and key if needed.

### Step 4: Create `SOUL.md`

Copy `SOUL.md` to `~/.hermes/profiles/media-requests/SOUL.md`.

### Step 5: Install the watchdog script

```bash
cp scripts/download_watchdog.py ~/.hermes/scripts/download_watchdog.py
cp scripts/download_watchdog.py ~/.hermes/profiles/media-requests/scripts/download_watchdog.py
mkdir -p ~/.hermes/profiles/media-requests/state
```

### Step 6: Install the systemd service

```bash
cp hermes-media-bot.service ~/.config/systemd/user/hermes-media-bot.service
systemctl --user daemon-reload
systemctl --user enable hermes-media-bot.service
systemctl --user start hermes-media-bot.service
```

### Step 7: Set up the watchdog cron

In a Hermes session (main profile), tell hermes:
```
Schedule a cron job every 3 minutes running python3 ~/.hermes/scripts/download_watchdog.py
```

### Step 8: Remove slash commands from the bot

```bash
BOT_TOKEN="your_bot_token"

curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{"commands":[]}'

curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setMyCommands" \
  -H "Content-Type: application/json" \
  -d '{"commands":[],"scope":{"type":"all_group_chats"}}'
```

### Step 9: Add bot to Telegram group

1. Add @grabbot to the "Media" Telegram group
2. Make sure `require_mention: true` is set (bot only responds when @mentioned)
3. Test: `@grabbot get The Matrix`

## Key Design Decisions

| Decision | Why |
|----------|-----|
| Separate Hermes profile | Isolates bot from main hermes instance |
| Separate systemd service | Independent lifecycle, restart policies, env vars |
| SOUL.md as sole guardrail | Terminal access is real, system prompt is the only safety barrier |
| Yolo mode approvals | Approval prompts are terrible UX for a group bot |
| Tirith disabled | Security scanner flags internal IPs the bot constantly talks to |
| Queue-based watchdog | Only triggers after arr import is truly complete, not just SAB download |
| Seerr-first workflow | Simplifies requests, avoids exposing arr internals |
| `GATEWAY_ALLOW_ALL_USERS=true` | Any group member can interact without pre-registration |
| `require_mention: true` | Prevents bot from responding to every message in the group |
| No slash commands | Cleaner UX — just @mention and say what you want |
| Memory disabled | Group users shouldn't persist personal data |
| Delegation disabled | Bot shouldn't spawn sub-agents |

## File Locations

```
~/.hermes/profiles/media-requests/
├── config.yaml              # Profile config
├── SOUL.md                  # Bot persona & rules
├── .env                     # Secrets
├── scripts/
│   └── download_watchdog.py # Watchdog (also in ~/.hermes/scripts/)
├── state/
│   └── watchdog_state.json  # Watchdog state (auto-created)
└── skills/
    └── media-request-flow/  # API reference skill

~/.config/systemd/user/
└── hermes-media-bot.service # Systemd unit
```

## Troubleshooting

```bash
# Check bot service status
systemctl --user status hermes-media-bot.service

# View bot logs
journalctl --user -u hermes-media-bot.service -f

# Test watchdog manually
python3 ~/.hermes/scripts/download_watchdog.py

# Check watchdog state
cat ~/.hermes/profiles/media-requests/state/watchdog_state.json

# Check arr queue (what watchdog is tracking)
curl -s "http://10.0.60.88:7878/api/v3/queue?apikey=YOUR_KEY&pageSize=20"

# Test Telegram bot token
curl -s "https://api.telegram.org/botYOUR_TOKEN/getMe"
```

## Future Plans

- Anime support (needs indexers configured in Prowlarr)
- Per-user request tracking
- Download progress updates in chat
