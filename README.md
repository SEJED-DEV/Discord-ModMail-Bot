# Cortex Modmail Core

A high-performance, resilient Discord Modmail system engineered for professional community management. Featuring **Automated Session Recovery**, this core ensures no tickets or transcripts are lost during bot restarts or infrastructure updates.

---

## 🚀 Key Features

* **Session Persistence:** Automatically restores active tickets from Discord metadata on startup. No database required.
* **Intelligent Routing:** Sanitized channel naming (`#ticket-username`) for instant staff recognition.
* **Media Support:** Inline image/GIF rendering and secure attachment handling for both users and staff.
* **Privacy Controls:** Support for Anonymous Staff Replies (`!anonreply`) to protect moderator identities.
* **Audit-Ready:** Generates clean, timestamped `.txt` transcripts automatically upon closure.

---

## 🛠️ Infrastructure & Support

Cortex Modmail Core is part of the **Cortex** ecosystem. For deployment assistance, technical documentation, or high-performance hosting, join our hub.

### 🛰️ **CORTEX**
The comprehensive Discord infrastructure solution. Verified, lightweight, and engineered for scale.

* **Verified Security:** Officially verified by Discord with high-tier uptime and robust data protection.
* **Unified Systems:** Advanced Moderation, Economy, Ticketing, and Engagement flows.
* **Official Links:** * [Add Cortex to Server](https://discord.com/oauth2/authorize?client_id=1481721720099569848)
    * [Cortex Website](https://cortex-bot.vercel.app)
    * [Top.gg Listing](https://top.gg/bot/1481721720099569848)

### ☁️ **High-Performance Hosting via CortexNodes**
I provide dedicated hosting for this Modmail bot and other community tools. **CortexNodes** provides users with **2 Free Servers** to get their infrastructure running immediately with zero overhead.

💬 **Join the Support Hub:** [Cortex Support Server](https://discord.gg/gkBfyk45ec)

---

## 📖 Full Setup Guide

### 1. Prerequisites
* **Python 3.8+** installed on your system.
* **Discord Developer Portal:** Create an application, add a bot, and enable the **Message Content Intent**.

### 2. Configuration
Open `modmail.py` and update the following constants to match your server setup:

| Constant | Description |
| :--- | :--- |
| `MODMAIL_CATEGORY_NAME` | The name of the category where ticket channels will live. |
| `LOG_CHANNEL_NAME` | The name of the channel where transcripts will be sent. |
| `STAFF_ROLE_NAME` | The exact name of your moderator/support role. |
| `DISCORD_BOT_TOKEN` | You can find it at the end of the `modmail.py` file. |

### 3. Deployment
Install the required library and run the bot:
```bash
pip install discord.py
python modmail.py
```

### 4. Initialization
Once the bot is online, go to your server and run:
`!setup`
The bot will automatically create the Modmail category and the logging channel with the correct permissions.

---

## 🛡️ Staff Commands

| Command | Usage | Description |
| :--- | :--- | :--- |
| `!reply` | `!reply <msg>` | Sends a message (and any attachments) to the user. |
| `!anonreply` | `!anonreply <msg>` | Sends a message anonymously (Staff name hidden). |
| `!close` | `!close [reason]` | Ends the session, saves the transcript, and deletes the channel. |
| `!claim` | `!claim` | Assigns the ticket to the current staff member. |
| `!ticketinfo` | `!ticketinfo` | View duration, message count, and user metadata. |
| `!blacklist` | `!blacklist <user>` | Restricts a user from opening new tickets. |

---

*Developed by Sejed Trabelssi for the Cortex Infrastructure Project. Focused on sovereign, high-performance community tools.*
