import discord
from discord.ext import commands, tasks
import asyncio
import datetime
import logging
import sys
import io
import re
import json
import os
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE = "modmail.log"
DATA_DIR = "modmail_data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
SNIPPETS_FILE = os.path.join(DATA_DIR, "snippets.json")
TRANSCRIPTS_DIR = os.path.join(DATA_DIR, "transcripts")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))
STAFF_ROLE_NAME = os.getenv("STAFF_ROLE_NAME", "Your_support_team_role")
MODMAIL_CATEGORY_NAME = os.getenv("MODMAIL_CATEGORY_NAME", "Cortex ModMail")
LOG_CHANNEL_NAME = os.getenv("LOG_CHANNEL_NAME", "modmail-logs")

STAFF_PING_ON_OPEN = os.getenv("STAFF_PING_ON_OPEN", "true").lower() == "true"
DM_COOLDOWN_SECONDS = int(os.getenv("DM_COOLDOWN_SECONDS", "5"))
AUTO_CLOSE_HOURS = int(os.getenv("AUTO_CLOSE_HOURS", "48"))
AUTO_CLOSE_GRACE_HOURS = int(os.getenv("AUTO_CLOSE_GRACE_HOURS", "24"))
AUTO_CLOSE_CHECK_MINUTES = 30
MAX_CHANNELS_PER_CATEGORY = 50

AUTO_SAVE_INTERVAL = 60
BACKUP_INTERVAL = 3600

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.WARNING,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("modmail")
logger.setLevel(logging.INFO)
for handler in logging.root.handlers:
    handler.addFilter(lambda record: record.name == "modmail")
logging.getLogger("discord").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════
# BOT SETUP
# ═══════════════════════════════════════════════════════════════════════════

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# In-memory state
open_tickets: dict[int, dict] = {}
claimed_tickets: dict[int, str] = {}
ticket_messages: dict[int, list] = defaultdict(list)
blacklisted_users: set[int] = set()
snippets: dict[str, str] = {}

# Locks, rate limits & pending prompt tracking
_ticket_open_locks: dict[int, asyncio.Lock] = {}
_dm_cooldowns: dict[int, datetime.datetime] = {}
_pending_open: set[int] = set()  # Users who've been shown the open-ticket prompt

bot_start_time = datetime.datetime.now(datetime.timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# CORE UTILITIES
# ═══════════════════════════════════════════════════════════════════════════

def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def log(level: str, message: str):
    getattr(logger, level.lower(), logger.info)(message)


def sanitize_channel_name(username: str) -> str:
    name = username.lower()
    name = re.sub(r'[^a-z0-9-]', '-', name)
    name = re.sub(r'-+', '-', name)
    return (name.strip('-') or "user")[:80]


def build_embed(title, description=None, color=discord.Color.blurple(),
                fields=None, footer=None, thumbnail=None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color, timestamp=now())
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(description=f"> {description}", color=discord.Color.red(), timestamp=now())


def success_embed(description: str) -> discord.Embed:
    return discord.Embed(description=f"> {description}", color=discord.Color.green(), timestamp=now())


def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if STAFF_ROLE_ID:
        return any(r.id == STAFF_ROLE_ID for r in member.roles)
    return any(r.name == STAFF_ROLE_NAME for r in member.roles)


def _is_modmail_category_name(name: str) -> bool:
    """Return True if the category name is a modmail category (including overflow)."""
    return name == MODMAIL_CATEGORY_NAME or bool(
        re.match(r"^" + re.escape(MODMAIL_CATEGORY_NAME) + r" \d+$", name)
    )


def is_ticket_channel(channel: discord.TextChannel) -> bool:
    """Return True only if `channel` is inside any modmail category."""
    return channel.category is not None and _is_modmail_category_name(channel.category.name)


def get_ticket_owner(channel) -> int | None:
    if channel.topic:
        match = re.search(r'\((\d{15,20})\)', channel.topic)
        if match:
            return int(match.group(1))
    match = re.match(r"ticket-\d+$", channel.name)
    if match:
        try:
            return int(channel.name.split("-")[1])
        except (IndexError, ValueError):
            pass
    return None


def get_ticket_channel(guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
    for category in guild.categories:
        if not _is_modmail_category_name(category.name):
            continue
        for channel in category.text_channels:
            if get_ticket_owner(channel) == user_id:
                return channel
    return None


def get_staff_role(guild: discord.Guild) -> discord.Role | None:
    if STAFF_ROLE_ID:
        return guild.get_role(STAFF_ROLE_ID)
    return discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)


# ═══════════════════════════════════════════════════════════════════════════
# PERSISTENT STORAGE
# ═══════════════════════════════════════════════════════════════════════════

def ensure_data_directory():
    Path(DATA_DIR).mkdir(exist_ok=True)
    Path(TRANSCRIPTS_DIR).mkdir(exist_ok=True)


def deserialize_datetime(s: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(s) if s else None


def save_state() -> bool:
    try:
        state: dict = {
            "open_tickets": {},
            "claimed_tickets": {},
            "ticket_messages": {},
            "last_save": now().isoformat()
        }
        for uid, ticket in open_tickets.items():
            state["open_tickets"][str(uid)] = {
                "channel_id": ticket["channel_id"],
                "guild_id": ticket["guild_id"],
                "opened_at": ticket["opened_at"].isoformat(),
                "last_activity": ticket.get("last_activity", ticket["opened_at"]).isoformat(),
                "close_warning_sent": ticket.get("close_warning_sent", False),
                "tags": ticket.get("tags", [])
            }
        for uid, claimer in claimed_tickets.items():
            state["claimed_tickets"][str(uid)] = claimer
        for uid, msgs in ticket_messages.items():
            state["ticket_messages"][str(uid)] = [
                {
                    "sender": m["sender"],
                    "content": m["content"],
                    "timestamp": m["timestamp"].isoformat(),
                    "anonymous": m.get("anonymous", False),
                    "note": m.get("note", False)
                }
                for m in msgs
            ]
        temp = STATE_FILE + ".tmp"
        with open(temp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(temp, STATE_FILE)
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to save state: {e}")
        return False


def load_state() -> bool:
    global open_tickets, claimed_tickets, ticket_messages
    if not os.path.exists(STATE_FILE):
        return False
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        open_tickets.clear()
        for uid_str, t in state.get("open_tickets", {}).items():
            uid = int(uid_str)
            open_tickets[uid] = {
                "channel_id": t["channel_id"],
                "guild_id": t["guild_id"],
                "opened_at": deserialize_datetime(t["opened_at"]),
                "last_activity": deserialize_datetime(t.get("last_activity") or t["opened_at"]),
                "close_warning_sent": t.get("close_warning_sent", False),
                "tags": t.get("tags", [])
            }
        claimed_tickets.clear()
        for k, v in state.get("claimed_tickets", {}).items():
            claimed_tickets[int(k)] = v
        ticket_messages.clear()
        for uid_str, msgs in state.get("ticket_messages", {}).items():
            uid = int(uid_str)
            ticket_messages[uid] = [
                {
                    "sender": m["sender"],
                    "content": m["content"],
                    "timestamp": deserialize_datetime(m["timestamp"]),
                    "anonymous": m.get("anonymous", False),
                    "note": m.get("note", False)
                }
                for m in msgs
            ]
        log("info", f"[STORAGE] State loaded: {len(open_tickets)} tickets")
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to load state: {e}")
        return False


def save_blacklist() -> bool:
    try:
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump({"blacklisted": list(blacklisted_users)}, f, indent=2)
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to save blacklist: {e}")
        return False


def load_blacklist() -> bool:
    global blacklisted_users
    if not os.path.exists(BLACKLIST_FILE):
        return False
    try:
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        blacklisted_users = set(int(x) for x in data.get("blacklisted", []))
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to load blacklist: {e}")
        return False


def load_snippets():
    global snippets
    if not os.path.exists(SNIPPETS_FILE):
        return
    try:
        with open(SNIPPETS_FILE, 'r', encoding='utf-8') as f:
            snippets = json.load(f)
        log("info", f"[STORAGE] Snippets loaded ({len(snippets)})")
    except Exception as e:
        log("error", f"[STORAGE] Failed to load snippets: {e}")


def save_snippets():
    try:
        with open(SNIPPETS_FILE, 'w', encoding='utf-8') as f:
            json.dump(snippets, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log("error", f"[STORAGE] Failed to save snippets: {e}")


def save_transcript_txt(user_id: int, transcript_text: str) -> str | None:
    try:
        ts = now().strftime("%Y%m%d-%H%M%S")
        fp = os.path.join(TRANSCRIPTS_DIR, f"transcript-{user_id}-{ts}.txt")
        with open(fp, 'w', encoding='utf-8') as f:
            f.write(transcript_text)
        return fp
    except Exception as e:
        log("error", f"[TRANSCRIPT] Failed to save .txt: {e}")
        return None


def save_transcript_html(user_id: int, html_content: str) -> str | None:
    try:
        ts = now().strftime("%Y%m%d-%H%M%S")
        fp = os.path.join(TRANSCRIPTS_DIR, f"transcript-{user_id}-{ts}.html")
        with open(fp, 'w', encoding='utf-8') as f:
            f.write(html_content)
        return fp
    except Exception as e:
        log("error", f"[TRANSCRIPT] Failed to save .html: {e}")
        return None


def create_backup() -> bool:
    try:
        if not os.path.exists(STATE_FILE):
            return False
        import shutil
        ts = now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(DATA_DIR, f"state_backup_{ts}.json")
        shutil.copy2(STATE_FILE, backup)
        backups = sorted(f for f in os.listdir(DATA_DIR) if f.startswith("state_backup_"))
        for old in backups[:-10]:
            os.remove(os.path.join(DATA_DIR, old))
        return True
    except Exception as e:
        log("error", f"[BACKUP] Failed: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# TRANSCRIPT BUILDERS
# ═══════════════════════════════════════════════════════════════════════════

async def build_txt_transcript(user, user_id: int, messages: list) -> str:
    user_display = str(user) if user else f"Unknown ({user_id})"
    lines = [
        "MODMAIL TRANSCRIPT",
        f"User: {user_display} ({user_id})",
        f"Generated: {now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "=" * 60
    ]
    for m in messages:
        ts = m["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if m.get("timestamp") else "?"
        anon = " [ANONYMOUS]" if m.get("anonymous") else ""
        note = " [INTERNAL NOTE]" if m.get("note") else ""
        lines.append(f"[{ts}] {m['sender']}{anon}{note}: {m['content']}")
    return "\n".join(lines)


def build_html_transcript(user, user_id: int, messages: list,
                          reason: str = "Closed", closed_by: str = "Staff",
                          tags: list | None = None, opened_at=None) -> str:
    username = str(user) if user else f"Unknown ({user_id})"
    avatar_url = str(user.display_avatar.url) if user and hasattr(user, "display_avatar") else ""
    user_str = str(user) if user else None
    opened_str = opened_at.strftime("%Y-%m-%d %H:%M:%S UTC") if opened_at else "Unknown"
    closed_str = now().strftime("%Y-%m-%d %H:%M:%S UTC")
    tags_html = "".join(f'<span class="tag">{t}</span>' for t in (tags or []))

    msg_parts = []
    for m in messages:
        is_note = m.get("note", False)
        is_anon = m.get("anonymous", False)
        sender = m["sender"]
        content = (m.get("content") or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        ts = m["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC") if m.get("timestamp") else ""

        if is_note:
            cls, label_cls = "note", "note-label"
        elif is_anon or (user_str and sender != user_str):
            cls, label_cls = "staff", "staff-label"
        else:
            cls, label_cls = "user", "user-label"

        badges = ""
        if is_anon:
            badges += '<span class="badge anon">Anonymous</span>'
        if is_note:
            badges += '<span class="badge note-badge">Internal Note</span>'

        msg_parts.append(f"""
        <div class="message {cls}">
            <div class="msg-header"><span class="{label_cls}">{sender}</span>{badges}<span class="timestamp">{ts}</span></div>
            <div class="content">{content}</div>
        </div>""")

    avatar_tag = (f"<img class='avatar' src='{avatar_url}' onerror=\"this.style.display='none'\" alt=''>"
                  if avatar_url else "<div class='avatar'></div>")

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ModMail Transcript — {username}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#313338;color:#dbdee1;font-family:'Segoe UI',Arial,sans-serif;padding:24px;max-width:900px;margin:0 auto}}
.header{{background:#2b2d31;border-radius:8px;padding:20px 24px;margin-bottom:14px;display:flex;align-items:center;gap:16px;border-left:4px solid #5865f2}}
.avatar{{width:56px;height:56px;border-radius:50%;object-fit:cover;background:#5865f2;flex-shrink:0}}
.header-info h1{{font-size:20px;font-weight:700;color:#f2f3f5}}.header-info p{{font-size:13px;color:#949ba4;margin-top:4px}}
.meta{{background:#2b2d31;border-radius:8px;padding:14px 20px;margin-bottom:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}}
.meta-item .label{{color:#949ba4;font-size:11px;text-transform:uppercase;font-weight:600;margin-bottom:2px}}
.meta-item .value{{color:#dbdee1;font-size:13px}}
.tags{{margin-bottom:14px;display:flex;flex-wrap:wrap;gap:6px}}
.tag{{background:#5865f2;color:#fff;padding:3px 10px;border-radius:12px;font-size:12px;font-weight:600}}
.messages{{display:flex;flex-direction:column;gap:6px}}
.message{{background:#2b2d31;border-radius:6px;padding:10px 14px}}
.message.user{{border-left:4px solid #5865f2}}.message.staff{{border-left:4px solid #f0b232}}
.message.note{{border-left:4px solid #57f287;background:#1a2420}}
.msg-header{{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}}
.user-label{{color:#5865f2;font-weight:700;font-size:14px}}
.staff-label{{color:#f0b232;font-weight:700;font-size:14px}}
.note-label{{color:#57f287;font-weight:700;font-size:14px}}
.timestamp{{color:#949ba4;font-size:11px;margin-left:auto}}
.content{{font-size:14px;line-height:1.6;word-break:break-word}}
.badge{{display:inline-block;padding:2px 7px;border-radius:10px;font-size:11px;font-weight:600}}
.badge.anon{{background:#4e5058;color:#dbdee1}}.badge.note-badge{{background:#57f287;color:#1a1a1a}}
.footer{{margin-top:24px;text-align:center;color:#4e5058;font-size:12px}}
</style></head>
<body>
<div class="header">{avatar_tag}<div class="header-info"><h1>ModMail Transcript</h1><p>{username} &bull; ID: {user_id}</p></div></div>
<div class="meta">
<div class="meta-item"><div class="label">Opened</div><div class="value">{opened_str}</div></div>
<div class="meta-item"><div class="label">Closed</div><div class="value">{closed_str}</div></div>
<div class="meta-item"><div class="label">Closed By</div><div class="value">{closed_by}</div></div>
<div class="meta-item"><div class="label">Reason</div><div class="value">{reason}</div></div>
<div class="meta-item"><div class="label">Messages</div><div class="value">{len(messages)}</div></div>
</div>
{f'<div class="tags">{tags_html}</div>' if tags_html else ""}
<div class="messages">{"".join(msg_parts)}</div>
<div class="footer">Generated by Cortex ModMail &bull; {now().strftime("%Y-%m-%d %H:%M:%S UTC")}</div>
</body></html>"""


# ═══════════════════════════════════════════════════════════════════════════
# SHARED TICKET OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

async def get_or_create_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Return a modmail category with room for a new channel. Creates overflow if needed."""
    all_cats = sorted(
        [c for c in guild.categories if _is_modmail_category_name(c.name)],
        key=lambda c: c.name
    )
    for cat in all_cats:
        if len(cat.channels) < MAX_CHANNELS_PER_CATEGORY:
            return cat

    num = len(all_cats) + 1
    name = MODMAIL_CATEGORY_NAME if num == 1 else f"{MODMAIL_CATEGORY_NAME} {num}"
    staff_role = get_staff_role(guild)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    category = await guild.create_category(name, overwrites=overwrites)
    log("info", f"[SETUP] Created overflow category '{name}' in {guild}")
    return category


async def log_to_discord(guild: discord.Guild, embed: discord.Embed,
                         files: list[discord.File] | None = None):
    channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if channel:
        try:
            if files:
                await channel.send(embed=embed, files=files)
            else:
                await channel.send(embed=embed)
        except discord.Forbidden:
            log("warning", f"[LOG] Cannot send to log channel in {guild}")


async def open_ticket(guild: discord.Guild, user: discord.User, first_message: str | None = None):
    """Open a new modmail ticket. Returns (channel, status)."""
    if user.id in open_tickets:
        return None, "already_open"

    category = await get_or_create_category(guild)
    staff_role = get_staff_role(guild)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True, manage_messages=True),
    }
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    safe_name = sanitize_channel_name(user.name)
    channel = await guild.create_text_channel(
        name=f"ticket-{safe_name}",
        category=category,
        overwrites=overwrites,
        topic=f"Modmail ticket for {user} ({user.id}) | Opened: {now().strftime('%Y-%m-%d %H:%M UTC')}"
    )

    open_tickets[user.id] = {
        "channel_id": channel.id,
        "guild_id": guild.id,
        "opened_at": now(),
        "last_activity": now(),
        "close_warning_sent": False,
        "tags": []
    }
    ticket_messages[user.id] = []

    fields = [
        ("User", f"{user.mention} (`{user}`)", True),
        ("User ID", str(user.id), True),
        ("Account Age", user.created_at.strftime("%B %d, %Y"), True),
        ("Commands", "`!reply <msg>` · `!anonreply <msg>` · `!close [reason]` · `!transcript`", False),
    ]
    embed = build_embed(
        "Modmail Ticket Opened",
        first_message or "No initial message provided.",
        color=discord.Color.green(),
        fields=fields,
        thumbnail=user.display_avatar.url,
        footer="Use !close to close this ticket"
    )
    await channel.send(embed=embed)

    if STAFF_PING_ON_OPEN and staff_role:
        await channel.send(f"{staff_role.mention} — New ticket opened.", delete_after=10)

    if first_message:
        ticket_messages[user.id].append({
            "sender": str(user),
            "content": first_message,
            "timestamp": now(),
            "anonymous": False,
            "note": False
        })

    save_state()
    log("info", f"[TICKET OPEN] {user} ({user.id}) | #{channel.name} | {guild}")
    await log_to_discord(guild, build_embed(
        "Ticket Opened",
        f"**User:** {user.mention} (`{user}`)\n**Channel:** {channel.mention}",
        color=discord.Color.green(),
        footer=f"User ID: {user.id}"
    ))
    return channel, "ok"


async def perform_close_ticket(user_id: int, reason: str = "No reason provided",
                                closed_by_name: str = "Staff",
                                is_user_close: bool = False,
                                is_auto: bool = False) -> bool:
    """Shared ticket closure logic. Safe to call from any context."""
    ticket = open_tickets.get(user_id)
    if not ticket:
        return False

    guild = bot.get_guild(ticket["guild_id"])
    if not guild:
        return False

    channel = bot.get_channel(ticket["channel_id"])
    try:
        user = await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException):
        user = None

    messages = ticket_messages.get(user_id, [])
    tags = ticket.get("tags", [])
    opened_at = ticket.get("opened_at")

    # Build both transcript formats
    ts_stamp = now().strftime('%Y%m%d-%H%M%S')
    txt = await build_txt_transcript(user, user_id, messages)
    html = build_html_transcript(user, user_id, messages,
                                 reason=reason, closed_by=closed_by_name,
                                 tags=tags, opened_at=opened_at)
    saved_txt = save_transcript_txt(user_id, txt)
    saved_html = save_transcript_html(user_id, html)

    # Post to log channel with BOTH files attached
    close_type = "Auto-Closed" if is_auto else ("User Closed" if is_user_close else "Staff Closed")
    log_embed = build_embed(
        f"Ticket {close_type}",
        f"**User:** `{user}` (`{user_id}`)"
        f"\n**Closed by:** {closed_by_name}"
        f"\n**Reason:** {reason}"
        f"\n**Messages:** {len(messages)}"
        f"\n**Tags:** {', '.join(tags) if tags else 'None'}"
        f"\n\n📄 **TXT:** `{saved_txt or 'failed to save'}`"
        f"\n🌐 **HTML:** `{saved_html or 'failed to save'}`",
        color=discord.Color.red(),
        footer="Both transcript files attached below"
    )
    log_files = []
    log_files.append(discord.File(
        fp=io.BytesIO(txt.encode("utf-8")),
        filename=f"transcript-{user_id}-{ts_stamp}.txt"
    ))
    log_files.append(discord.File(
        fp=io.BytesIO(html.encode("utf-8")),
        filename=f"transcript-{user_id}-{ts_stamp}.html"
    ))
    await log_to_discord(guild, log_embed, files=log_files)

    # DM the user — close embed + HTML transcript
    if user:
        try:
            await user.send(embed=build_embed(
                "Ticket Closed",
                f"Your modmail ticket has been closed.\n**Reason:** {reason}\n\n"
                "A full transcript of your conversation is attached below.\n"
                "You may open a new ticket by sending another DM.",
                color=discord.Color.red(),
                footer=guild.name
            ), file=discord.File(
                fp=io.BytesIO(html.encode("utf-8")),
                filename=f"transcript-{user_id}-{ts_stamp}.html"
            ))
        except discord.Forbidden:
            pass

    # Clean state
    open_tickets.pop(user_id, None)
    claimed_tickets.pop(user_id, None)
    ticket_messages.pop(user_id, None)
    save_state()

    log("info", f"[TICKET CLOSE] uid={user_id} | by={closed_by_name} | reason={reason}")

    # Notify and delete channel
    if channel:
        closer_label = "inactivity" if is_auto else ("the user" if is_user_close else closed_by_name)
        try:
            await channel.send(embed=success_embed(f"Ticket closed by **{closer_label}**. Transcripts saved."))
            await asyncio.sleep(4)
            await channel.delete(reason=f"Ticket closed by {closed_by_name}")
        except (discord.Forbidden, discord.NotFound):
            pass

    return True


async def send_with_images(destination, embed: discord.Embed, attachments: list):
    await destination.send(embed=embed)
    for att in attachments:
        if any(att.filename.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
            img_embed = discord.Embed(color=embed.color, timestamp=now())
            img_embed.set_image(url=att.url)
            await destination.send(embed=img_embed)
        else:
            await destination.send(f"📎 **Attachment:** {att.url}")


# ═══════════════════════════════════════════════════════════════════════════
# DISCORD UI VIEWS  (persistent — survive bot restarts)
# ═══════════════════════════════════════════════════════════════════════════

class TicketOpenView(discord.ui.View):
    """Prompt sent to a user when they first DM the bot."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green,
                       emoji="📬", custom_id="modmail:open_ticket")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        _pending_open.discard(user.id)

        if user.id in open_tickets:
            await interaction.response.edit_message(
                embed=error_embed("You already have an open ticket. Reply here to continue."),
                view=None
            )
            return

        await interaction.response.edit_message(
            embed=build_embed("Opening ticket…", "Please wait a moment.", color=discord.Color.yellow()),
            view=None
        )

        guild = bot.get_guild(GUILD_ID) if GUILD_ID else (bot.guilds[0] if bot.guilds else None)
        if not guild:
            await interaction.edit_original_response(embed=error_embed("Unable to process your request. Please try again later."))
            return

        lock = _ticket_open_locks.setdefault(user.id, asyncio.Lock())
        async with lock:
            if user.id not in open_tickets:
                channel, status = await open_ticket(guild, user)
                if status == "ok":
                    await interaction.edit_original_response(
                        embed=build_embed(
                            "✅ Ticket Opened",
                            "Your ticket is now open. Please send your question or issue and a staff member will assist you shortly.",
                            color=discord.Color.green(),
                            footer="Reply here to continue the conversation"
                        )
                    )
                    # Send close button in a separate message
                    await interaction.followup.send(
                        embed=build_embed(
                            "Close Your Ticket",
                            "Once your issue is resolved, you can close your ticket below.",
                            color=discord.Color.blurple()
                        ),
                        view=UserCloseView()
                    )
                else:
                    await interaction.edit_original_response(embed=error_embed("Failed to open ticket. Please try again."))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red,
                       emoji="✖️", custom_id="modmail:cancel_open")
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        _pending_open.discard(interaction.user.id)
        await interaction.response.edit_message(
            embed=build_embed("Cancelled", "No ticket was opened.", color=discord.Color.red()),
            view=None
        )


class UserCloseView(discord.ui.View):
    """Button sent in the user's DM so they can self-close their ticket."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close My Ticket", style=discord.ButtonStyle.red,
                       emoji="🔒", custom_id="modmail:user_close")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user
        if user.id not in open_tickets:
            await interaction.response.edit_message(
                embed=build_embed("No Open Ticket", "You don't have an open ticket.", color=discord.Color.red()),
                view=None
            )
            return
        await interaction.response.edit_message(
            embed=build_embed("Closing…", "Your ticket is being closed.", color=discord.Color.orange()),
            view=None
        )
        await perform_close_ticket(user.id, reason="Closed by user", closed_by_name=str(user), is_user_close=True)
        await interaction.edit_original_response(
            embed=build_embed("Ticket Closed", "Your ticket has been closed. You may open a new one by sending a DM.", color=discord.Color.red())
        )


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════════════════

@tasks.loop(seconds=AUTO_SAVE_INTERVAL)
async def auto_save_task():
    if open_tickets or ticket_messages:
        save_state()


@tasks.loop(seconds=BACKUP_INTERVAL)
async def backup_task():
    create_backup()


@tasks.loop(minutes=AUTO_CLOSE_CHECK_MINUTES)
async def auto_close_task():
    """Warn about inactive tickets, then auto-close after grace period."""
    for user_id, ticket in list(open_tickets.items()):
        last_activity = ticket.get("last_activity", ticket["opened_at"])
        inactive_hours = (now() - last_activity).total_seconds() / 3600

        if not ticket.get("close_warning_sent") and inactive_hours >= AUTO_CLOSE_HOURS:
            # Send inactivity warning
            try:
                user = await bot.fetch_user(user_id)
                await user.send(embed=build_embed(
                    "⚠️ Ticket Inactivity Warning",
                    f"Your modmail ticket has been inactive for **{AUTO_CLOSE_HOURS} hours**.\n\n"
                    f"It will be **automatically closed in {AUTO_CLOSE_GRACE_HOURS} hours** "
                    f"if there is no response.\n\nReply here to keep it open.",
                    color=discord.Color.orange()
                ))
            except (discord.Forbidden, discord.NotFound):
                pass

            ticket["close_warning_sent"] = True
            save_state()

            channel = bot.get_channel(ticket["channel_id"])
            if channel:
                await channel.send(embed=build_embed(
                    "⚠️ Inactivity Warning Sent",
                    f"User was warned. Ticket auto-closes in {AUTO_CLOSE_GRACE_HOURS}h without a response.",
                    color=discord.Color.orange()
                ))

        elif ticket.get("close_warning_sent") and inactive_hours >= (AUTO_CLOSE_HOURS + AUTO_CLOSE_GRACE_HOURS):
            log("info", f"[AUTO-CLOSE] Closing inactive ticket for user {user_id}")
            await perform_close_ticket(user_id, reason="Auto-closed due to inactivity",
                                       closed_by_name="System", is_auto=True)


@auto_save_task.before_loop
async def _before_auto_save():
    await bot.wait_until_ready()


@backup_task.before_loop
async def _before_backup():
    await bot.wait_until_ready()


@auto_close_task.before_loop
async def _before_auto_close():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════
# STARTUP VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def validate_config():
    """Log warnings for any missing configuration values."""
    warnings = []
    if not GUILD_ID:
        warnings.append("GUILD_ID is not set — using fallback (first guild). Set this in .env for production!")
    if not STAFF_ROLE_ID:
        warnings.append(f"STAFF_ROLE_ID is not set — falling back to role name '{STAFF_ROLE_NAME}'. Set STAFF_ROLE_ID for reliability.")
    if AUTO_CLOSE_HOURS < 1:
        warnings.append("AUTO_CLOSE_HOURS is less than 1 — auto-close will trigger very quickly!")
    for w in warnings:
        log("warning", f"[CONFIG] ⚠️  {w}")
    if not warnings:
        log("info", "[CONFIG] ✅ All configuration values look good.")


# ═══════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    ensure_data_directory()
    load_blacklist()
    load_state()
    load_snippets()
    validate_config()

    # Register persistent views so buttons survive restarts
    bot.add_view(TicketOpenView())
    bot.add_view(UserCloseView())

    # Restore / verify ticket channels
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else (bot.guilds[0] if bot.guilds else None)
    restored = verified = 0

    if guild:
        for category in guild.categories:
            if not _is_modmail_category_name(category.name):
                continue
            for channel in category.text_channels:
                user_id = get_ticket_owner(channel)
                if not user_id:
                    continue
                if user_id in open_tickets:
                    ticket = open_tickets[user_id]
                    if ticket["channel_id"] != channel.id:
                        ticket["channel_id"] = channel.id
                    verified += 1
                else:
                    open_tickets[user_id] = {
                        "channel_id": channel.id,
                        "guild_id": guild.id,
                        "opened_at": now(),
                        "last_activity": now(),
                        "close_warning_sent": False,
                        "tags": []
                    }
                    ticket_messages.setdefault(user_id, [])
                    restored += 1

                # Rename old ticket-{user_id} channels
                if re.match(r"^ticket-\d+$", channel.name):
                    try:
                        u = await bot.fetch_user(user_id)
                        await channel.edit(name=f"ticket-{sanitize_channel_name(u.name)}")
                    except Exception:
                        pass

        if restored:
            save_state()
    else:
        log("error", "[STARTUP] Bot is not in any guild! Invite it and run !setup.")

    # Start background tasks
    for task in (auto_save_task, backup_task, auto_close_task):
        if not task.is_running():
            task.start()

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="DMs for modmail"),
        status=discord.Status.online
    )
    log("info", f"[READY] {bot.user} | {guild} | tickets={len(open_tickets)} restored={restored} verified={verified}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed(f"Missing argument: `{error.param.name}`."))
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.CheckFailure):
        pass  # Silent — don't expose staff-only commands
    else:
        await ctx.send(embed=error_embed(f"An error occurred: `{error}`"))
        log("error", f"[ERROR] {ctx.author} | !{ctx.command} | {error}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm(message)
        return
    await bot.process_commands(message)


@bot.event
async def on_guild_channel_delete(channel):
    """Clean up state when a ticket channel is deleted manually."""
    if not isinstance(channel, discord.TextChannel):
        return
    for uid, ticket in list(open_tickets.items()):
        if ticket["channel_id"] == channel.id:
            open_tickets.pop(uid, None)
            ticket_messages.pop(uid, None)
            claimed_tickets.pop(uid, None)
            save_state()
            log("info", f"[CLEANUP] Ghost ticket uid={uid} removed (channel deleted manually)")
            break


async def handle_dm(message):
    user = message.author
    content = message.content.strip()
    attachments = message.attachments

    if not content and not attachments:
        return

    # Rate limit
    last = _dm_cooldowns.get(user.id)
    if last and (now() - last).total_seconds() < DM_COOLDOWN_SECONDS:
        return
    _dm_cooldowns[user.id] = now()

    guild = bot.get_guild(GUILD_ID) if GUILD_ID else (bot.guilds[0] if bot.guilds else None)
    if not guild:
        return

    # Blacklist
    if user.id in blacklisted_users:
        await user.send(embed=error_embed("You are blacklisted from using modmail."))
        return

    # If user is not in a ticket yet
    if user.id not in open_tickets:
        # Don't show a second prompt if one is already pending
        if user.id in _pending_open:
            return
        _pending_open.add(user.id)
        await user.send(
            embed=build_embed(
                "📬 Contact Staff",
                "Would you like to open a modmail ticket?\n\n"
                "A member of the staff team will respond as soon as possible.\n"
                "Click **Open Ticket** to proceed or **Cancel** to dismiss.",
                color=discord.Color.blurple(),
                footer="This server uses Cortex ModMail"
            ),
            view=TicketOpenView()
        )
        return

    # Forward message to existing ticket
    ticket = open_tickets.get(user.id)
    if not ticket:
        return
    channel = bot.get_channel(ticket["channel_id"])
    if not channel:
        return

    embed = build_embed(
        f"Message from {user.name}",
        content or "*[Attachment only]*",
        color=discord.Color.blurple(),
        thumbnail=user.display_avatar.url,
        footer=f"{user} | {user.id}"
    )
    await send_with_images(channel, embed, attachments)

    display_content = content
    if attachments:
        display_content += "\n" + "\n".join(a.url for a in attachments)

    ticket_messages[user.id].append({
        "sender": str(user),
        "content": display_content,
        "timestamp": now(),
        "anonymous": False,
        "note": False
    })

    # Update activity timestamp and reset warning
    ticket["last_activity"] = now()
    ticket["close_warning_sent"] = False
    save_state()

    await message.add_reaction("✅")
    log("info", f"[DM] {user} → #{channel.name}")


# ═══════════════════════════════════════════════════════════════════════════
# STAFF COMMAND GUARD
# ═══════════════════════════════════════════════════════════════════════════

def staff_only():
    async def predicate(ctx):
        return (
            isinstance(ctx.channel, discord.TextChannel)
            and is_staff(ctx.author)
            and is_ticket_channel(ctx.channel)
        )
    return commands.check(predicate)


# ═══════════════════════════════════════════════════════════════════════════
# STAFF COMMANDS  (ticket channels only)
# ═══════════════════════════════════════════════════════════════════════════

@bot.command()
@staff_only()
async def reply(ctx, *, message: str = ""):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    attachments = ctx.message.attachments
    if not message and not attachments:
        return await ctx.send(embed=error_embed("Provide a message or attach a file."))
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Could not find this ticket's user."))

    embed = build_embed("Staff Reply", message or "*[Attachment only]*",
                        color=discord.Color.gold(),
                        footer=f"From: {ctx.author.name} | {ctx.guild.name}")
    try:
        await send_with_images(user, embed, attachments)
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Could not DM the user (DMs disabled)."))

    confirm = build_embed(f"Reply sent by {ctx.author.name}", message or "*[Attachment only]*",
                          color=discord.Color.gold(), footer=f"Delivered to {user}")
    await send_with_images(ctx.channel, confirm, attachments)
    await ctx.message.delete()

    content = message + ("\n" + "\n".join(a.url for a in attachments) if attachments else "")
    ticket_messages[user_id].append({"sender": str(ctx.author), "content": content,
                                     "timestamp": now(), "anonymous": False, "note": False})
    open_tickets[user_id]["last_activity"] = now()
    save_state()
    log("info", f"[REPLY] {ctx.author} → uid={user_id}")


@bot.command()
@staff_only()
async def anonreply(ctx, *, message: str = ""):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    attachments = ctx.message.attachments
    if not message and not attachments:
        return await ctx.send(embed=error_embed("Provide a message or attach a file."))
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Could not find this ticket's user."))

    embed = build_embed("Staff Reply", message or "*[Attachment only]*",
                        color=discord.Color.gold(),
                        footer=f"From: Staff Team | {ctx.guild.name}")
    try:
        await send_with_images(user, embed, attachments)
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Could not DM the user (DMs disabled)."))

    confirm = build_embed(f"Anonymous reply by {ctx.author.name}", message or "*[Attachment only]*",
                          color=discord.Color.dark_gold(), footer=f"Delivered anonymously to {user}")
    await send_with_images(ctx.channel, confirm, attachments)
    await ctx.message.delete()

    content = message + ("\n" + "\n".join(a.url for a in attachments) if attachments else "")
    ticket_messages[user_id].append({"sender": str(ctx.author), "content": content,
                                     "timestamp": now(), "anonymous": True, "note": False})
    open_tickets[user_id]["last_activity"] = now()
    save_state()
    log("info", f"[ANON REPLY] {ctx.author} → uid={user_id}")


@bot.command()
@staff_only()
async def note(ctx, *, text: str):
    """Add an internal staff note — never shown to the user."""
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))

    ticket_messages[user_id].append({
        "sender": str(ctx.author),
        "content": text,
        "timestamp": now(),
        "anonymous": False,
        "note": True
    })
    save_state()
    await ctx.message.delete()
    embed = build_embed(
        f"📝 Internal Note — {ctx.author.name}",
        text,
        color=discord.Color.from_str("#57f287"),
        footer="This note is NOT visible to the user"
    )
    await ctx.send(embed=embed)
    log("info", f"[NOTE] {ctx.author} added note to uid={user_id}")


@bot.command()
@staff_only()
async def close(ctx, *, reason: str = "No reason provided"):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    await ctx.message.delete()
    await perform_close_ticket(user_id, reason=reason, closed_by_name=str(ctx.author))


@bot.command()
@staff_only()
async def transcript(ctx):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        user = None
    msgs = ticket_messages.get(user_id, [])
    if not msgs:
        return await ctx.send(embed=error_embed("No messages recorded yet."))
    txt = await build_txt_transcript(user, user_id, msgs)
    file = discord.File(fp=io.BytesIO(txt.encode("utf-8")),
                        filename=f"transcript-{user_id}-{now().strftime('%Y%m%d-%H%M%S')}.txt")
    await ctx.send(embed=success_embed("Transcript generated."), file=file)


@bot.command()
@staff_only()
async def ticketinfo(ctx):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    ticket = open_tickets[user_id]
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        user = None

    delta = now() - ticket["opened_at"]
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    tags = ticket.get("tags", [])

    fields = [
        ("User", user.mention if user else str(user_id), True),
        ("User ID", str(user_id), True),
        ("Opened", ticket["opened_at"].strftime("%B %d, %Y %H:%M UTC"), True),
        ("Duration", f"{h}h {m}m", True),
        ("Messages", str(len(ticket_messages.get(user_id, []))), True),
        ("Claimed By", str(claimed_tickets.get(user_id, "Unclaimed")), True),
        ("Tags", ", ".join(tags) if tags else "None", True),
        ("Auto-close Warning", "Sent" if ticket.get("close_warning_sent") else "Not sent", True),
    ]
    embed = build_embed("Ticket Information", color=discord.Color.blurple(), fields=fields,
                        thumbnail=user.display_avatar.url if user else None)
    await ctx.send(embed=embed)


@bot.command()
@staff_only()
async def claim(ctx):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    if user_id in claimed_tickets:
        return await ctx.send(embed=error_embed(f"Already claimed by **{claimed_tickets[user_id]}**."))
    claimed_tickets[user_id] = str(ctx.author)
    save_state()
    await ctx.send(embed=success_embed(f"Ticket claimed by {ctx.author.mention}."))


@bot.command()
@staff_only()
async def unclaim(ctx):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    if user_id not in claimed_tickets:
        return await ctx.send(embed=error_embed("This ticket has not been claimed."))
    claimed_tickets.pop(user_id)
    save_state()
    await ctx.send(embed=success_embed("Ticket unclaimed."))


@bot.command()
@staff_only()
async def tag(ctx, *, tag_name: str):
    """Add a tag to this ticket."""
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    clean = tag_name.lower().strip().replace(" ", "-")[:32]
    tags: list = open_tickets[user_id].setdefault("tags", [])
    if clean in tags:
        return await ctx.send(embed=error_embed(f"Tag `{clean}` is already on this ticket."))
    tags.append(clean)
    save_state()
    # Reflect in channel topic
    try:
        base_topic = re.sub(r" \| Tags:.*$", "", ctx.channel.topic or "")
        await ctx.channel.edit(topic=f"{base_topic} | Tags: {', '.join(tags)}")
    except discord.Forbidden:
        pass
    await ctx.send(embed=success_embed(f"Tag `{clean}` added."))


@bot.command()
@staff_only()
async def untag(ctx, *, tag_name: str):
    """Remove a tag from this ticket."""
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    clean = tag_name.lower().strip().replace(" ", "-")[:32]
    tags: list = open_tickets[user_id].get("tags", [])
    if clean not in tags:
        return await ctx.send(embed=error_embed(f"Tag `{clean}` not found on this ticket."))
    tags.remove(clean)
    save_state()
    try:
        base_topic = re.sub(r" \| Tags:.*$", "", ctx.channel.topic or "")
        new_topic = f"{base_topic} | Tags: {', '.join(tags)}" if tags else base_topic
        await ctx.channel.edit(topic=new_topic)
    except discord.Forbidden:
        pass
    await ctx.send(embed=success_embed(f"Tag `{clean}` removed."))


@bot.command()
@staff_only()
async def snippet(ctx, key: str):
    """Send a pre-written snippet as a staff reply."""
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))
    text = snippets.get(key.lower())
    if not text:
        available = ", ".join(f"`{k}`" for k in snippets) or "None"
        return await ctx.send(embed=error_embed(f"Snippet `{key}` not found. Available: {available}"))
    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Could not find the user for this ticket."))

    embed = build_embed("Staff Reply", text, color=discord.Color.gold(),
                        footer=f"From: {ctx.author.name} | {ctx.guild.name}")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Could not DM the user (DMs disabled)."))

    confirm = build_embed(f"Snippet `{key}` sent by {ctx.author.name}", text,
                          color=discord.Color.gold(), footer=f"Delivered to {user}")
    await ctx.send(embed=confirm)
    await ctx.message.delete()

    ticket_messages[user_id].append({"sender": str(ctx.author), "content": text,
                                     "timestamp": now(), "anonymous": False, "note": False})
    open_tickets[user_id]["last_activity"] = now()
    save_state()


@bot.command()
@staff_only()
async def blacklist(ctx, user: discord.User, *, reason: str = "No reason provided"):
    blacklisted_users.add(user.id)
    save_blacklist()
    await ctx.send(embed=success_embed(f"**{user}** blacklisted. Reason: {reason}"))


@bot.command()
@staff_only()
async def unblacklist(ctx, user: discord.User):
    if user.id not in blacklisted_users:
        return await ctx.send(embed=error_embed(f"**{user}** is not blacklisted."))
    blacklisted_users.discard(user.id)
    save_blacklist()
    await ctx.send(embed=success_embed(f"**{user}** removed from blacklist."))


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS  (admin permission required)
# ═══════════════════════════════════════════════════════════════════════════

@bot.command()
@commands.has_permissions(administrator=True)
async def addsnippet(ctx, key: str, *, text: str):
    """Add or update a canned response snippet."""
    key = key.lower().strip()
    snippets[key] = text
    save_snippets()
    await ctx.send(embed=success_embed(f"Snippet `{key}` saved."))


@bot.command()
@commands.has_permissions(administrator=True)
async def delsnippet(ctx, key: str):
    """Delete a snippet."""
    key = key.lower().strip()
    if key not in snippets:
        return await ctx.send(embed=error_embed(f"Snippet `{key}` not found."))
    snippets.pop(key)
    save_snippets()
    await ctx.send(embed=success_embed(f"Snippet `{key}` deleted."))


@bot.command()
@commands.has_permissions(administrator=True)
async def snippets_list(ctx):
    """List all snippets."""
    if not snippets:
        return await ctx.send(embed=build_embed("Snippets", "No snippets configured yet.\n Use `!addsnippet <key> <text>` to create one.", color=discord.Color.blurple()))
    fields = [(k, v[:200] + ("…" if len(v) > 200 else ""), False) for k, v in snippets.items()]
    embed = build_embed(f"Snippets ({len(snippets)})",
                        "Use `!snippet <key>` inside a ticket to send.",
                        color=discord.Color.blurple(), fields=fields[:25])
    await ctx.send(embed=embed)


@bot.command(name="snippets")
@commands.has_permissions(administrator=True)
async def snippets_cmd(ctx):
    await ctx.invoke(snippets_list)


@bot.command()
@commands.has_permissions(administrator=True)
async def forcesave(ctx):
    ok = save_state()
    await ctx.send(embed=success_embed(f"State saved. Tickets: {len(open_tickets)}") if ok
                   else error_embed("Failed to save state. Check logs."))


@bot.command()
@commands.has_permissions(administrator=True)
async def forcebackup(ctx):
    ok = create_backup()
    await ctx.send(embed=success_embed("Backup created.") if ok
                   else error_embed("Backup failed. Check logs."))


@bot.command()
@commands.has_permissions(administrator=True)
async def botstats(ctx):
    uptime = now() - bot_start_time
    h, rem = divmod(int(uptime.total_seconds()), 3600)
    m, _ = divmod(rem, 60)
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    state_size = os.path.getsize(STATE_FILE) / 1024 if os.path.exists(STATE_FILE) else 0
    transcript_count = len(os.listdir(TRANSCRIPTS_DIR)) if os.path.exists(TRANSCRIPTS_DIR) else 0

    fields = [
        ("Uptime", f"{h}h {m}m", True),
        ("Open Tickets", str(len(open_tickets)), True),
        ("Claimed Tickets", str(len(claimed_tickets)), True),
        ("Active Messages", str(sum(len(v) for v in ticket_messages.values())), True),
        ("Blacklisted Users", str(len(blacklisted_users)), True),
        ("Snippets", str(len(snippets)), True),
        ("Saved Transcripts", str(transcript_count), True),
        ("State File", f"{state_size:.1f} KB", True),
        ("Auto-save", "✅ Running" if auto_save_task.is_running() else "❌ Stopped", True),
        ("Auto-close", "✅ Running" if auto_close_task.is_running() else "❌ Stopped", True),
        ("Server", str(guild) if guild else "Unknown", True),
        ("GUILD_ID", str(GUILD_ID) if GUILD_ID else "⚠️ Not set", True),
    ]
    await ctx.send(embed=build_embed("Bot Statistics", "Modmail system health",
                                     color=discord.Color.blue(), fields=fields,
                                     footer=f"Data directory: {DATA_DIR}"))


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    category = await get_or_create_category(ctx.guild)
    staff_role = get_staff_role(ctx.guild)
    log_channel = discord.utils.get(ctx.guild.text_channels, name=LOG_CHANNEL_NAME)

    if not log_channel:
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True)
        log_channel = await ctx.guild.create_text_channel(LOG_CHANNEL_NAME, overwrites=overwrites)
    else:
        # Always refresh permissions
        ow = dict(log_channel.overwrites)
        ow[ctx.guild.default_role] = discord.PermissionOverwrite(read_messages=False)
        ow[ctx.guild.me] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if staff_role:
            ow[staff_role] = discord.PermissionOverwrite(read_messages=True)
        await log_channel.edit(overwrites=ow)

    fields = [
        ("Category", category.name, True),
        ("Log Channel", log_channel.mention, True),
        ("Staff Role", staff_role.mention if staff_role else f"⚠️ {STAFF_ROLE_NAME} (not found)", True),
        ("GUILD_ID", str(GUILD_ID) if GUILD_ID else "⚠️ Not set", True),
        ("STAFF_ROLE_ID", str(STAFF_ROLE_ID) if STAFF_ROLE_ID else "⚠️ Not set", True),
        ("Auto-close", f"After {AUTO_CLOSE_HOURS}h + {AUTO_CLOSE_GRACE_HOURS}h grace", True),
        ("Staff Ping", "✅ On open" if STAFF_PING_ON_OPEN else "❌ Disabled", True),
        ("Data Dir", DATA_DIR, False),
    ]
    await ctx.send(embed=build_embed("Modmail Setup Complete",
                                     "System ready with persistent storage, auto-close, and HTML transcripts.",
                                     color=discord.Color.green(), fields=fields))
    log("info", f"[SETUP] {ctx.author} ran setup in {ctx.guild}")


# ═══════════════════════════════════════════════════════════════════════════
# GENERAL COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

@bot.command()
async def help(ctx):
    if isinstance(ctx.channel, discord.DMChannel):
        await ctx.send(embed=build_embed(
            "Modmail Help",
            "To contact staff, simply send a message here.\n"
            "Your message will be forwarded to the moderation team.\n\n"
            "You can continue the conversation by replying in this DM.",
            color=discord.Color.blurple(),
            footer="Your message is private and only visible to staff"
        ))
        return

    if not is_staff(ctx.author) or not is_ticket_channel(ctx.channel):
        return

    fields = [
        ("!reply <message>", "Reply to the user (supports attachments)", False),
        ("!anonreply <message>", "Reply anonymously — identity hidden", False),
        ("!note <text>", "Add an internal note (staff-only, not sent to user)", False),
        ("!snippet <key>", "Send a pre-written canned response", False),
        ("!close [reason]", "Close the ticket and save transcripts", False),
        ("!transcript", "Generate a .txt transcript of this ticket", False),
        ("!ticketinfo", "View ticket metadata, tags, and status", False),
        ("!claim", "Claim this ticket", False),
        ("!unclaim", "Release your claim", False),
        ("!tag <tag>", "Add a tag to this ticket", False),
        ("!untag <tag>", "Remove a tag", False),
        ("!blacklist <user> [reason]", "Block a user from modmail", False),
        ("!unblacklist <user>", "Unblock a user", False),
        ("!opentickets", "List all open tickets", False),
        ("!staffstats", "Show claimed tickets per staff member", False),
    ]
    if ctx.author.guild_permissions.administrator:
        fields += [
            ("─" * 40, "**Admin Commands**", False),
            ("!addsnippet <key> <text>", "Create a snippet", False),
            ("!delsnippet <key>", "Delete a snippet", False),
            ("!snippets", "List all snippets", False),
            ("!setup", "Create/refresh categories and log channel", False),
            ("!forcesave", "Manually save state to disk", False),
            ("!forcebackup", "Create a state backup", False),
            ("!botstats", "Bot health dashboard", False),
        ]
    await ctx.send(embed=build_embed(
        "Modmail Staff Commands",
        "Commands available inside ticket channels.",
        color=discord.Color.blurple(),
        fields=fields,
        footer="Cortex ModMail v3.0"
    ))


@bot.command()
@staff_only()
async def opentickets(ctx):
    if not open_tickets:
        return await ctx.send(embed=build_embed("Open Tickets", "No tickets currently open.", color=discord.Color.green()))
    lines = []
    for uid, ticket in open_tickets.items():
        channel = bot.get_channel(ticket["channel_id"])
        claimed = claimed_tickets.get(uid, "Unclaimed")
        tags = ticket.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        ch_ref = channel.mention if channel else f"`#ticket-{uid}`"
        lines.append(f"{ch_ref}{tag_str} — **{claimed}** — {len(ticket_messages.get(uid, []))} msgs")
    MAX = 20
    if len(lines) > MAX:
        lines = lines[:MAX] + [f"*… and {len(open_tickets) - MAX} more.*"]
    await ctx.send(embed=build_embed(f"Open Tickets ({len(open_tickets)})",
                                     "\n".join(lines), color=discord.Color.blurple()))


@bot.command()
@staff_only()
async def staffstats(ctx):
    """Show how many tickets each staff member currently has claimed."""
    if not claimed_tickets:
        return await ctx.send(embed=build_embed("Staff Stats", "No tickets are currently claimed.", color=discord.Color.blurple()))

    counts: dict[str, int] = {}
    for claimer in claimed_tickets.values():
        counts[claimer] = counts.get(claimer, 0) + 1

    fields = [
        (name, f"**{count}** ticket(s) claimed", False)
        for name, count in sorted(counts.items(), key=lambda x: -x[1])
    ]
    await ctx.send(embed=build_embed(
        f"Staff Claim Stats",
        f"{len(claimed_tickets)} of {len(open_tickets)} open tickets are claimed.",
        color=discord.Color.blurple(),
        fields=fields
    ))


# ═══════════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_disconnect():
    log("warning", "[CONNECTION] Bot disconnected")


@bot.event
async def on_resumed():
    log("info", "[CONNECTION] Bot reconnected")


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("[FATAL] DISCORD_BOT_TOKEN is not set. Create a .env file — see .env.example")
        sys.exit(1)
    try:
        bot.run(token)
    except Exception as e:
        log("error", f"[FATAL] Bot crashed: {e}")
        save_state()
        save_blacklist()
