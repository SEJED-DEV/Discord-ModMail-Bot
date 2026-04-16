# Cortex ModMail

> **Advanced Discord Modmail Infrastructure** — Session Persistence · Auto-Close · HTML Transcripts · Canned Responses · Ticket Tagging

---

## ✨ What's New in v3.0

| Feature | Details |
|---------|---------|
| 🔘 **Button-based ticket open** | Users click a button to open a ticket — no accidental spam |
| 🔴 **User self-close button** | Users can close their own ticket from DMs |
| 📝 **Internal notes** | Staff-only notes stored in the ticket, shown in transcripts |
| 📋 **Canned responses** | Pre-written snippets triggered with `!snippet <key>` |
| 🏷️ **Ticket tagging** | Tag tickets (`ban-appeal`, `report`, etc.) for easy sorting |
| 📊 **Staff stats** | `!staffstats` shows claims per staff member |
| ⏰ **Auto-close** | Warns after inactivity, auto-closes after grace period |
| 🌐 **HTML transcripts** | Styled dark-theme HTML — saved to disk, attached to log channel, **and sent to the user** |
| 📂 **Category overflow** | Creates `Cortex ModMail 2`, `3`, etc. when 50-channel limit is hit |
| ✅ **Startup validation** | Warns on missing `GUILD_ID` / `STAFF_ROLE_ID` at startup |

---

## 🚀 Key Features

- **Persistent Session Storage** — Tickets, messages, tags, and claims survive restarts
- **Guaranteed Transcript Preservation** — `.txt` + `.html` attached to log channel, `.html` sent to the user, both saved to disk
- **Automated Session Recovery** — Restores all active tickets and history on startup
- **Intelligent Routing** — Sanitized channel naming (`#ticket-username`)
- **Media Support** — Inline image/GIF rendering for both users and staff
- **Anonymous Replies** — `!anonreply` hides the staff member's identity
- **Internal Notes** — `!note` stores staff commentary visible only in transcripts
- **Canned Responses** — `!snippet` sends pre-written replies instantly
- **Ticket Tags** — Categorize tickets (`ban-appeal`, `report`, `question`, etc.)
- **Auto-Close** — Inactive tickets warned then closed automatically
- **Rolling Backups** — Hourly backups with 10-backup rotation

---

## 🛠️ Installation

### 1. Prerequisites
- Python **3.10+**
- A Discord bot with **Message Content Intent** enabled  
  → [Discord Developer Portal](https://discord.com/developers/applications)

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Description |
|----------|-------------|
| `DISCORD_BOT_TOKEN` | Your bot token from the Developer Portal |
| `GUILD_ID` | Right-click server → **Copy Server ID** *(requires Developer Mode)* |
| `STAFF_ROLE_ID` | Right-click your staff role → **Copy Role ID** |
| `STAFF_ROLE_NAME` | Fallback display name if `STAFF_ROLE_ID` is not set |
| `MODMAIL_CATEGORY_NAME` | Category name (default: `Cortex ModMail`) |
| `LOG_CHANNEL_NAME` | Log channel name (default: `modmail-logs`) |
| `STAFF_PING_ON_OPEN` | Ping staff role on new ticket (`true`/`false`) |
| `DM_COOLDOWN_SECONDS` | Rate limit between DMs (default: `5`) |
| `AUTO_CLOSE_HOURS` | Inactivity hours before warning (default: `48`) |
| `AUTO_CLOSE_GRACE_HOURS` | Hours after warning before close (default: `24`) |

### 4. Run

```bash
python modmail.py
```

### 5. First-Time Setup

Run this command in any admin channel on your server:

```
!setup
```

The bot will create:
- The **Cortex ModMail** category with correct permissions
- The **#modmail-logs** channel restricted to staff
- Additional overflow categories automatically as needed (`Cortex ModMail 2`, etc.)

---

## 📁 Data Structure

On first run, the bot creates:

```
modmail_data/
├── state.json              ← Active tickets, claims, tags
├── blacklist.json          ← Blacklisted user IDs
├── snippets.json           ← Canned response library
├── state_backup_*.json     ← Rolling hourly backups (last 10)
└── transcripts/
    ├── transcript-{uid}-{ts}.txt    ← Plain text  → attached to #modmail-logs
    └── transcript-{uid}-{ts}.html   ← Styled HTML → attached to #modmail-logs + sent to user
```

---

## 💬 User Flow

1. User DMs the bot
2. Bot sends a **"Contact Staff"** prompt with **[Open Ticket]** and **[Cancel]** buttons
3. User clicks **Open Ticket** → private ticket channel created
4. Staff replies using `!reply` — user receives it in DMs
5. User can reply directly in DMs to continue the conversation
6. User or staff can close the ticket at any time
7. On close:
   - `.txt` + `.html` transcripts **attached to `#modmail-logs`** with disk paths shown in the embed
   - `.html` transcript **sent to the user in DMs** so they have a full record

---

## 👮 Staff Commands

> All commands below only work **inside ticket channels** in the modmail category.

| Command | Description |
|---------|-------------|
| `!reply <message>` | Reply to the user (supports attachments) |
| `!anonreply <message>` | Reply anonymously — identity hidden from user |
| `!note <text>` | Add an internal note (not sent to user, appears in transcript) |
| `!snippet <key>` | Send a pre-written canned response |
| `!close [reason]` | Close the ticket and save transcripts |
| `!transcript` | Generate a `.txt` transcript on demand |
| `!ticketinfo` | View ticket metadata (opened, duration, tags, claim status) |
| `!claim` | Claim this ticket as your own |
| `!unclaim` | Release your claim |
| `!tag <tag>` | Add a tag (e.g. `ban-appeal`, `report`) |
| `!untag <tag>` | Remove a tag |
| `!blacklist <@user> [reason]` | Prevent a user from opening tickets |
| `!unblacklist <@user>` | Remove a user from the blacklist |
| `!opentickets` | List all currently open tickets with tags and claims |
| `!staffstats` | Show claimed ticket count per staff member |

---

## 🔧 Admin Commands

> Require **Administrator** permission.

| Command | Description |
|---------|-------------|
| `!addsnippet <key> <text>` | Create or update a canned response snippet |
| `!delsnippet <key>` | Delete a snippet |
| `!snippets` | List all snippets |
| `!setup` | Create/refresh category, log channel, and permissions |
| `!forcesave` | Manually save state to disk |
| `!forcebackup` | Create a state backup immediately |
| `!botstats` | View bot uptime, ticket counts, and system health |

---

## ⏰ Auto-Close System

The bot checks for inactive tickets every **30 minutes**.

1. If a ticket has had no activity for `AUTO_CLOSE_HOURS` (default 48h):
   - A warning DM is sent to the user
   - The ticket channel is notified

2. If there is still no activity after `AUTO_CLOSE_GRACE_HOURS` (default 24h):
   - The ticket is **automatically closed** with a `System` label
   - Transcripts are saved normally

> Any message from the user **resets** the inactivity timer and clears the warning.

---

## 🏷️ Ticket Tagging

Tags are stored in the ticket data and shown in:
- `!ticketinfo`
- `!opentickets`  
- HTML and `.txt` transcripts
- The ticket channel topic

```
!tag ban-appeal
!tag urgent
!untag ban-appeal
```

---

## 📋 Canned Responses (Snippets)

Snippets are pre-written replies stored in `modmail_data/snippets.json`.

```
!addsnippet rules Please review our server rules at #rules before continuing.
!addsnippet appeal Ban appeals take up to 48 hours. We will review your case shortly.
!snippet rules        ← sends the "rules" snippet as a staff reply
!snippets             ← lists all available snippets
```

---

## 🌐 Cortex Ecosystem

Cortex ModMail is part of the **Cortex** infrastructure suite.

- [Add Cortex to Server](https://discord.com/oauth2/authorize?client_id=1481721720099569848)
- [Cortex Website](https://cortex-bot.vercel.app)
- [Support Server](https://discord.gg/gkBfyk45ec)
- [Top.gg Listing](https://top.gg/bot/1481721720099569848)
