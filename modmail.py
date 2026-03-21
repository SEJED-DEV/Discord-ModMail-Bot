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

# ═══════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

LOG_FILE = "modmail.log"
DATA_DIR = "modmail_data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")
TRANSCRIPTS_DIR = os.path.join(DATA_DIR, "transcripts")

MODMAIL_CATEGORY_NAME = "Valeria Modmail"
LOG_CHANNEL_NAME = "modmail-logs"
STAFF_ROLE_NAME = "[-]   Support Team"

AUTO_SAVE_INTERVAL = 60  # Auto-save every 60 seconds
BACKUP_INTERVAL = 3600   # Backup every hour

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
logging.getLogger("discord.http").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)

# ═══════════════════════════════════════════════════════════════════════════
# BOT SETUP
# ═══════════════════════════════════════════════════════════════════════════

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# In-memory state (backed by persistent storage)
open_tickets = {}
claimed_tickets = {}
ticket_messages = defaultdict(list)
blacklisted_users = set()

bot_start_time = datetime.datetime.now(datetime.timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# PERSISTENT STORAGE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def ensure_data_directory():
    """Create data directory structure if it doesn't exist."""
    Path(DATA_DIR).mkdir(exist_ok=True)
    Path(TRANSCRIPTS_DIR).mkdir(exist_ok=True)
    log("info", f"[STORAGE] Data directory ensured at {DATA_DIR}")


def serialize_datetime(obj):
    """JSON serializer for datetime objects."""
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")


def deserialize_datetime(date_string):
    """Convert ISO format string back to datetime."""
    if date_string:
        return datetime.datetime.fromisoformat(date_string)
    return None


def save_state():
    """Save all bot state to disk."""
    try:
        state = {
            "open_tickets": {},
            "claimed_tickets": claimed_tickets,
            "ticket_messages": {},
            "last_save": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        
        # Convert ticket data to serializable format
        for user_id, ticket in open_tickets.items():
            state["open_tickets"][str(user_id)] = {
                "channel_id": ticket["channel_id"],
                "guild_id": ticket["guild_id"],
                "opened_at": ticket["opened_at"].isoformat()
            }
        
        # Convert messages to serializable format
        for user_id, messages in ticket_messages.items():
            state["ticket_messages"][str(user_id)] = [
                {
                    "sender": msg["sender"],
                    "content": msg["content"],
                    "timestamp": msg["timestamp"].isoformat(),
                    "anonymous": msg.get("anonymous", False)
                }
                for msg in messages
            ]
        
        # Write to temp file first, then rename (atomic operation)
        temp_file = STATE_FILE + ".tmp"
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(temp_file, STATE_FILE)
        
        log("debug", f"[STORAGE] State saved successfully ({len(open_tickets)} tickets)")
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to save state: {e}")
        return False


def load_state():
    """Load bot state from disk."""
    global open_tickets, claimed_tickets, ticket_messages
    
    if not os.path.exists(STATE_FILE):
        log("info", "[STORAGE] No existing state file found, starting fresh")
        return False
    
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
        
        # Restore open tickets
        open_tickets.clear()
        for user_id_str, ticket in state.get("open_tickets", {}).items():
            user_id = int(user_id_str)
            open_tickets[user_id] = {
                "channel_id": ticket["channel_id"],
                "guild_id": ticket["guild_id"],
                "opened_at": deserialize_datetime(ticket["opened_at"])
            }
        
        # Restore claimed tickets
        claimed_tickets.clear()
        claimed_tickets.update(state.get("claimed_tickets", {}))
        
        # Restore messages
        ticket_messages.clear()
        for user_id_str, messages in state.get("ticket_messages", {}).items():
            user_id = int(user_id_str)
            ticket_messages[user_id] = [
                {
                    "sender": msg["sender"],
                    "content": msg["content"],
                    "timestamp": deserialize_datetime(msg["timestamp"]),
                    "anonymous": msg.get("anonymous", False)
                }
                for msg in messages
            ]
        
        last_save = state.get("last_save", "unknown")
        log("info", f"[STORAGE] State loaded successfully: {len(open_tickets)} tickets, last saved at {last_save}")
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to load state: {e}")
        return False


def save_blacklist():
    """Save blacklist to disk."""
    try:
        data = {"blacklisted": list(blacklisted_users)}
        with open(BLACKLIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        log("debug", f"[STORAGE] Blacklist saved ({len(blacklisted_users)} users)")
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to save blacklist: {e}")
        return False


def load_blacklist():
    """Load blacklist from disk."""
    global blacklisted_users
    
    if not os.path.exists(BLACKLIST_FILE):
        log("info", "[STORAGE] No existing blacklist file found")
        return False
    
    try:
        with open(BLACKLIST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        blacklisted_users = set(data.get("blacklisted", []))
        log("info", f"[STORAGE] Blacklist loaded ({len(blacklisted_users)} users)")
        return True
    except Exception as e:
        log("error", f"[STORAGE] Failed to load blacklist: {e}")
        return False


def save_transcript_to_file(user_id, user_name, transcript_text):
    """Save a transcript to a permanent file."""
    try:
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"transcript-{user_id}-{timestamp}.txt"
        filepath = os.path.join(TRANSCRIPTS_DIR, filename)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(transcript_text)
        
        log("info", f"[TRANSCRIPT] Saved to {filepath}")
        return filepath
    except Exception as e:
        log("error", f"[TRANSCRIPT] Failed to save transcript for {user_id}: {e}")
        return None


def create_backup():
    """Create a timestamped backup of the current state."""
    try:
        if not os.path.exists(STATE_FILE):
            return False
        
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_file = os.path.join(DATA_DIR, f"state_backup_{timestamp}.json")
        
        import shutil
        shutil.copy2(STATE_FILE, backup_file)
        
        log("info", f"[BACKUP] Created backup: {backup_file}")
        
        # Clean old backups (keep last 10)
        backups = sorted([f for f in os.listdir(DATA_DIR) if f.startswith("state_backup_")])
        if len(backups) > 10:
            for old_backup in backups[:-10]:
                os.remove(os.path.join(DATA_DIR, old_backup))
                log("debug", f"[BACKUP] Removed old backup: {old_backup}")
        
        return True
    except Exception as e:
        log("error", f"[BACKUP] Failed to create backup: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════════════════

@tasks.loop(seconds=AUTO_SAVE_INTERVAL)
async def auto_save_task():
    """Automatically save state at regular intervals."""
    if open_tickets or ticket_messages:
        save_state()


@tasks.loop(seconds=BACKUP_INTERVAL)
async def backup_task():
    """Create periodic backups."""
    create_backup()


@auto_save_task.before_loop
async def before_auto_save():
    await bot.wait_until_ready()


@backup_task.before_loop
async def before_backup():
    await bot.wait_until_ready()


# ═══════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def now():
    return datetime.datetime.now(datetime.timezone.utc)


def log(level, message):
    getattr(logger, level.lower(), logger.info)(message)


def sanitize_channel_name(username: str) -> str:
    """Convert a Discord username into a valid channel name segment."""
    name = username.lower()
    name = re.sub(r'[^a-z0-9-]', '-', name)
    name = re.sub(r'-+', '-', name)
    name = name.strip('-') or "user"
    return name[:80]


def build_embed(title, description=None, color=discord.Color.blurple(), fields=None, footer=None, thumbnail=None):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=now())
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


def error_embed(description):
    return discord.Embed(description=f"> {description}", color=discord.Color.red(), timestamp=now())


def success_embed(description):
    return discord.Embed(description=f"> {description}", color=discord.Color.green(), timestamp=now())


def is_staff(member):
    return any(r.name == STAFF_ROLE_NAME for r in member.roles) or member.guild_permissions.administrator


def get_ticket_channel(guild, user_id):
    """Find an open ticket channel for this user by scanning the modmail category."""
    category = discord.utils.get(guild.categories, name=MODMAIL_CATEGORY_NAME)
    if not category:
        return None
    for channel in category.text_channels:
        uid = get_ticket_owner(channel)
        if uid == user_id:
            return channel
    return None


def get_ticket_owner(channel) -> int | None:
    """
    Extract the ticket owner's user ID from a channel.
    Reads the channel topic first (most reliable), then falls back to
    legacy ticket-{user_id} channel name format.
    """
    # Primary: topic contains "(user_id)"
    if channel.topic:
        match = re.search(r'\((\d{15,20})\)', channel.topic)
        if match:
            return int(match.group(1))
    # Fallback: old ticket-{user_id} naming
    match = re.match(r"ticket-\d+$", channel.name)
    if match:
        try:
            return int(channel.name.split("-")[1])
        except (IndexError, ValueError):
            pass
    return None


async def get_or_create_category(guild):
    category = discord.utils.get(guild.categories, name=MODMAIL_CATEGORY_NAME)
    if not category:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        category = await guild.create_category(MODMAIL_CATEGORY_NAME, overwrites=overwrites)
        log("info", f"[SETUP] Created category '{MODMAIL_CATEGORY_NAME}' in {guild}")
    return category


async def log_to_discord(guild, embed):
    channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if channel:
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log("warning", f"[LOG] Cannot send to log channel in {guild}")


async def build_transcript(user, messages):
    lines = []
    lines.append(f"MODMAIL TRANSCRIPT")
    lines.append(f"User: {user} ({user.id})")
    lines.append(f"Generated: {now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("=" * 60)
    for entry in messages:
        timestamp = entry["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        sender = entry["sender"]
        content = entry["content"]
        anon = " [ANONYMOUS]" if entry.get("anonymous") else ""
        lines.append(f"[{timestamp}] {sender}{anon}: {content}")
    return "\n".join(lines)


async def send_with_images(destination, embed, attachments: list):
    """Send an embed and then display each image attachment as its own embed."""
    await destination.send(embed=embed)
    for attachment in attachments:
        if any(attachment.filename.lower().endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
            img_embed = discord.Embed(color=embed.color, timestamp=now())
            img_embed.set_image(url=attachment.url)
            await destination.send(embed=img_embed)
        else:
            # Non-image file — send as a link
            await destination.send(f"📎 **Attachment:** {attachment.url}")


async def open_ticket(guild, user, first_message=None, attachments=None):
    if user.id in open_tickets:
        return None, "already_open"

    category = await get_or_create_category(guild)

    staff_role = discord.utils.get(guild.roles, name=STAFF_ROLE_NAME)
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

    open_tickets[user.id] = {"channel_id": channel.id, "guild_id": guild.id, "opened_at": now()}
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
        footer=f"Use !close to close this ticket"
    )
    await send_with_images(channel, embed, attachments or [])

    if first_message:
        ticket_messages[user.id].append({
            "sender": str(user),
            "content": first_message,
            "timestamp": now(),
            "anonymous": False
        })

    # Save state immediately after opening ticket
    save_state()

    log("info", f"[TICKET OPEN] {user} ({user.id}) | Channel: #{channel.name} | Guild: {guild}")
    await log_to_discord(guild, build_embed(
        "Ticket Opened",
        f"**User:** {user.mention} (`{user}`)\n**Channel:** {channel.mention}",
        color=discord.Color.green(),
        footer=f"User ID: {user.id}"
    ))

    return channel, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    # ── Initialize storage ────────────────────────────────────────────────
    ensure_data_directory()
    
    # ── Load persistent data ──────────────────────────────────────────────
    load_blacklist()
    load_state()
    
    # ── Restore/verify tickets from channels ──────────────────────────────
    restored = 0
    verified = 0
    for guild in bot.guilds:
        category = discord.utils.get(guild.categories, name=MODMAIL_CATEGORY_NAME)
        if not category:
            continue
        
        for channel in category.text_channels:
            user_id = get_ticket_owner(channel)
            if not user_id:
                continue
            
            if user_id in open_tickets:
                # Verify existing ticket
                ticket = open_tickets[user_id]
                if ticket["channel_id"] != channel.id:
                    log("warning", f"[RESTORE] Channel ID mismatch for user {user_id}, updating")
                    ticket["channel_id"] = channel.id
                verified += 1
            else:
                # Restore ticket not in state
                open_tickets[user_id] = {
                    "channel_id": channel.id,
                    "guild_id": guild.id,
                    "opened_at": now()
                }
                if user_id not in ticket_messages:
                    ticket_messages[user_id] = []
                restored += 1
                log("info", f"[RESTORE] Restored ticket for user ID {user_id} from #{channel.name}")
            
            # ── Rename old ticket-{user_id} channels ──────────────────────
            if re.match(r"^ticket-\d+$", channel.name):
                try:
                    user = await bot.fetch_user(user_id)
                    safe_name = sanitize_channel_name(user.name)
                    new_name = f"ticket-{safe_name}"
                    await channel.edit(name=new_name)
                    log("info", f"[RENAME] #{channel.name} → #{new_name} for user {user}")
                except Exception as e:
                    log("warning", f"[RENAME] Could not rename #{channel.name}: {e}")
    
    # Save state after restoration
    if restored > 0:
        save_state()
    
    # ── Start background tasks ────────────────────────────────────────────
    if not auto_save_task.is_running():
        auto_save_task.start()
        log("info", "[TASKS] Auto-save task started")
    
    if not backup_task.is_running():
        backup_task.start()
        log("info", "[TASKS] Backup task started")
    
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="DMs for modmail"),
        status=discord.Status.online
    )
    
    log("info", f"Modmail bot online: {bot.user} | Guilds: {len(bot.guilds)} | Tickets: {len(open_tickets)} (restored: {restored}, verified: {verified})")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed(f"Missing argument: `{error.param.name}`. Use `!help` for usage."))
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(embed=error_embed("You do not have permission to use this command."))
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


async def handle_dm(message):
    user = message.author
    content = message.content.strip()
    attachments = message.attachments

    if not content and not attachments:
        return

    guild = None
    for g in bot.guilds:
        guild = g
        break

    if not guild:
        return

    # Check blacklist
    if user.id in blacklisted_users:
        await user.send(embed=error_embed("You are blacklisted from using modmail."))
        log("info", f"[BLACKLIST] Blocked DM from {user} ({user.id})")
        return

    if user.id not in open_tickets:
        channel, status = await open_ticket(guild, user, content or "[Attachment]", attachments)
        if status == "ok":
            confirm = build_embed(
                "Ticket Opened",
                "Your message has been received by the staff team. Please wait for a response.\n\n"
                "You can continue sending messages here and they will be forwarded.",
                color=discord.Color.green(),
                footer="Reply here to continue the conversation"
            )
            await user.send(embed=confirm)
        return

    ticket = open_tickets.get(user.id)
    if not ticket:
        return

    channel = bot.get_channel(ticket["channel_id"])
    if not channel:
        log("error", f"[DM] Could not find channel {ticket['channel_id']} for ticket of user {user.id}")
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
        "anonymous": False
    })

    # Save state after receiving message
    save_state()

    log("info", f"[DM] {user} ({user.id}) sent a message to ticket #{channel.name}")
    await message.add_reaction("✅")


# ═══════════════════════════════════════════════════════════════════════════
# STAFF COMMANDS (used inside ticket channels)
# ═══════════════════════════════════════════════════════════════════════════

def staff_only():
    async def predicate(ctx):
        return isinstance(ctx.channel, discord.TextChannel) and is_staff(ctx.author)
    return commands.check(predicate)


@bot.command()
@staff_only()
async def reply(ctx, *, message: str = ""):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))

    attachments = ctx.message.attachments
    if not message and not attachments:
        return await ctx.send(embed=error_embed("Please provide a message or attach a file."))

    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Could not find the user for this ticket."))

    embed = build_embed(
        "Staff Reply",
        message or "*[Attachment only]*",
        color=discord.Color.gold(),
        footer=f"From: {ctx.author.name} | {ctx.guild.name}"
    )
    try:
        await send_with_images(user, embed, attachments)
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Could not DM the user. They may have DMs disabled."))

    confirm = build_embed(
        f"Reply sent by {ctx.author.name}",
        message or "*[Attachment only]*",
        color=discord.Color.gold(),
        footer=f"Delivered to {user}"
    )
    await send_with_images(ctx.channel, confirm, attachments)
    await ctx.message.delete()

    display_content = message
    if attachments:
        display_content += "\n" + "\n".join(a.url for a in attachments)

    ticket_messages[user_id].append({
        "sender": str(ctx.author),
        "content": display_content,
        "timestamp": now(),
        "anonymous": False
    })

    # Save state after reply
    save_state()

    log("info", f"[REPLY] {ctx.author} replied to ticket for {user} ({user_id})")


@bot.command()
@staff_only()
async def anonreply(ctx, *, message: str = ""):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))

    attachments = ctx.message.attachments
    if not message and not attachments:
        return await ctx.send(embed=error_embed("Please provide a message or attach a file."))

    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return await ctx.send(embed=error_embed("Could not find the user for this ticket."))

    embed = build_embed(
        "Staff Reply",
        message or "*[Attachment only]*",
        color=discord.Color.gold(),
        footer=f"From: Staff Team | {ctx.guild.name}"
    )
    try:
        await send_with_images(user, embed, attachments)
    except discord.Forbidden:
        return await ctx.send(embed=error_embed("Could not DM the user. They may have DMs disabled."))

    confirm = build_embed(
        f"Anonymous reply sent by {ctx.author.name}",
        message or "*[Attachment only]*",
        color=discord.Color.dark_gold(),
        footer=f"Delivered anonymously to {user}"
    )
    await send_with_images(ctx.channel, confirm, attachments)
    await ctx.message.delete()

    display_content = message
    if attachments:
        display_content += "\n" + "\n".join(a.url for a in attachments)

    ticket_messages[user_id].append({
        "sender": str(ctx.author),
        "content": display_content,
        "timestamp": now(),
        "anonymous": True
    })

    # Save state after anonymous reply
    save_state()

    log("info", f"[ANON REPLY] {ctx.author} sent anonymous reply to ticket for {user} ({user_id})")


@bot.command()
@staff_only()
async def close(ctx, *, reason: str = "No reason provided"):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))

    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        user = None

    # Build and save transcript
    messages = ticket_messages.get(user_id, [])
    transcript_text = await build_transcript(user or ctx.author, messages)
    
    # Save to persistent storage
    saved_path = save_transcript_to_file(user_id, str(user) if user else "unknown", transcript_text)
    
    transcript_file = discord.File(
        fp=io.BytesIO(transcript_text.encode("utf-8")),
        filename=f"transcript-{user_id}-{now().strftime('%Y%m%d-%H%M%S')}.txt"
    )

    log_channel = discord.utils.get(ctx.guild.text_channels, name=LOG_CHANNEL_NAME)
    if log_channel:
        log_embed = build_embed(
            "Ticket Closed",
            f"**User:** `{user}` (`{user_id}`)\n"
            f"**Closed by:** {ctx.author.mention}\n"
            f"**Reason:** {reason}\n"
            f"**Messages:** {len(messages)}\n"
            f"**Saved:** `{saved_path or 'Failed to save'}`",
            color=discord.Color.red(),
            footer=f"Transcript attached and saved to disk"
        )
        await log_channel.send(embed=log_embed, file=transcript_file)

    if user:
        try:
            close_embed = build_embed(
                "Ticket Closed",
                f"Your modmail ticket has been closed.\n**Reason:** {reason}\n\nYou may open a new ticket by sending another DM.",
                color=discord.Color.red(),
                footer=ctx.guild.name
            )
            await user.send(embed=close_embed)
        except discord.Forbidden:
            log("warning", f"[CLOSE] Could not DM user {user} ({user_id}) about ticket closure")

    # Remove ticket from state
    open_tickets.pop(user_id, None)
    claimed_tickets.pop(user_id, None)
    ticket_messages.pop(user_id, None)

    # Save state immediately after closing
    save_state()

    await ctx.send(embed=success_embed(f"Ticket closed. Transcript saved to {log_channel.mention if log_channel else '#' + LOG_CHANNEL_NAME} and disk."))
    log("info", f"[TICKET CLOSE] {ctx.author} closed ticket for user ID {user_id} | Reason: {reason}")

    await asyncio.sleep(3)
    await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")


@bot.command()
@staff_only()
async def transcript(ctx):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))

    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        user = ctx.author

    messages = ticket_messages.get(user_id, [])
    if not messages:
        return await ctx.send(embed=error_embed("No messages have been recorded in this ticket yet."))

    transcript_text = await build_transcript(user, messages)
    transcript_file = discord.File(
        fp=io.BytesIO(transcript_text.encode("utf-8")),
        filename=f"transcript-{user_id}-{now().strftime('%Y%m%d-%H%M%S')}.txt"
    )
    await ctx.send(embed=success_embed("Transcript generated."), file=transcript_file)
    log("info", f"[TRANSCRIPT] {ctx.author} generated transcript for ticket of user {user_id}")


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

    opened_at = ticket["opened_at"]
    delta = now() - opened_at
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    claimed_by = claimed_tickets.get(user_id, "Unclaimed")

    fields = [
        ("User", f"{user.mention if user else user_id}", True),
        ("User ID", str(user_id), True),
        ("Opened", opened_at.strftime("%B %d, %Y %H:%M UTC"), True),
        ("Duration", f"{hours}h {minutes}m", True),
        ("Messages", str(len(ticket_messages.get(user_id, []))), True),
        ("Claimed By", str(claimed_by), True),
    ]
    thumbnail = user.display_avatar.url if user else None
    embed = build_embed("Ticket Information", color=discord.Color.blurple(), fields=fields, thumbnail=thumbnail)
    await ctx.send(embed=embed)


@bot.command()
@staff_only()
async def claim(ctx):
    user_id = get_ticket_owner(ctx.channel)
    if not user_id or user_id not in open_tickets:
        return await ctx.send(embed=error_embed("This is not an active modmail ticket channel."))

    if user_id in claimed_tickets:
        return await ctx.send(embed=error_embed(f"This ticket is already claimed by **{claimed_tickets[user_id]}**."))

    claimed_tickets[user_id] = str(ctx.author)
    save_state()
    await ctx.send(embed=success_embed(f"Ticket claimed by {ctx.author.mention}."))
    log("info", f"[CLAIM] {ctx.author} claimed ticket for user ID {user_id}")


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
    log("info", f"[UNCLAIM] {ctx.author} unclaimed ticket for user ID {user_id}")


@bot.command()
@staff_only()
async def blacklist(ctx, user: discord.User, *, reason: str = "No reason provided"):
    blacklisted_users.add(user.id)
    save_blacklist()
    await ctx.send(embed=success_embed(f"**{user}** has been blacklisted from modmail.\nReason: {reason}"))
    log("info", f"[BLACKLIST] {ctx.author} blacklisted {user} ({user.id}) | Reason: {reason}")


@bot.command()
@staff_only()
async def unblacklist(ctx, user: discord.User):
    if user.id not in blacklisted_users:
        return await ctx.send(embed=error_embed(f"**{user}** is not blacklisted."))
    blacklisted_users.discard(user.id)
    save_blacklist()
    await ctx.send(embed=success_embed(f"**{user}** has been removed from the blacklist."))
    log("info", f"[UNBLACKLIST] {ctx.author} unblacklisted {user} ({user.id})")


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

@bot.command()
@commands.has_permissions(administrator=True)
async def forcesave(ctx):
    """Manually trigger a state save."""
    success = save_state()
    if success:
        await ctx.send(embed=success_embed(f"State saved successfully. Tickets: {len(open_tickets)}"))
    else:
        await ctx.send(embed=error_embed("Failed to save state. Check logs."))


@bot.command()
@commands.has_permissions(administrator=True)
async def forcebackup(ctx):
    """Manually create a backup."""
    success = create_backup()
    if success:
        await ctx.send(embed=success_embed("Backup created successfully."))
    else:
        await ctx.send(embed=error_embed("Failed to create backup. Check logs."))


@bot.command()
@commands.has_permissions(administrator=True)
async def botstats(ctx):
    """Display bot statistics and health."""
    uptime = now() - bot_start_time
    hours, remainder = divmod(int(uptime.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)
    
    total_messages = sum(len(msgs) for msgs in ticket_messages.values())
    
    state_exists = os.path.exists(STATE_FILE)
    state_size = os.path.getsize(STATE_FILE) if state_exists else 0
    
    fields = [
        ("Bot Uptime", f"{hours}h {minutes}m", True),
        ("Open Tickets", str(len(open_tickets)), True),
        ("Total Messages", str(total_messages), True),
        ("Claimed Tickets", str(len(claimed_tickets)), True),
        ("Blacklisted Users", str(len(blacklisted_users)), True),
        ("Guilds", str(len(bot.guilds)), True),
        ("State File", f"{state_size / 1024:.1f} KB" if state_exists else "Not found", True),
        ("Auto-save", "Running" if auto_save_task.is_running() else "Stopped", True),
        ("Backup Task", "Running" if backup_task.is_running() else "Stopped", True),
    ]
    
    embed = build_embed(
        "Bot Statistics",
        f"Modmail system health and metrics",
        color=discord.Color.blue(),
        fields=fields,
        footer=f"Data directory: {DATA_DIR}"
    )
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════
# GENERAL COMMANDS
# ═══════════════════════════════════════════════════════════════════════════

@bot.command()
async def help(ctx):
    if isinstance(ctx.channel, discord.DMChannel):
        embed = build_embed(
            "Modmail Help",
            "To contact staff, simply send a message here.\n"
            "Your message will be forwarded to the moderation team.\n\n"
            "You can continue the conversation by replying in this DM.",
            color=discord.Color.blurple(),
            footer="Your message is private and only visible to staff"
        )
        await ctx.send(embed=embed)
        return

    if not is_staff(ctx.author):
        return

    fields = [
        ("!reply <message>", "Reply to the user in this ticket (supports attachments)", False),
        ("!anonreply <message>", "Reply anonymously — name hidden from user (supports attachments)", False),
        ("!close [reason]", "Close the ticket and save a transcript", False),
        ("!transcript", "Generate a transcript of this ticket", False),
        ("!ticketinfo", "View ticket details and metadata", False),
        ("!claim", "Claim this ticket as your own", False),
        ("!unclaim", "Release your claim on this ticket", False),
        ("!blacklist <user> [reason]", "Prevent a user from opening tickets", False),
        ("!unblacklist <user>", "Remove a user from the blacklist", False),
        ("!opentickets", "List all currently open tickets", False),
        ("!setup", "Create the modmail category and log channel", False),
    ]
    
    if ctx.author.guild_permissions.administrator:
        fields.extend([
            ("─" * 40, "**Admin Commands**", False),
            ("!forcesave", "Manually save bot state to disk", False),
            ("!forcebackup", "Create a backup of the current state", False),
            ("!botstats", "Display bot statistics and health", False),
        ])
    
    embed = build_embed(
        "Modmail Staff Commands",
        "These commands are only available inside ticket channels.",
        color=discord.Color.blurple(),
        fields=fields,
        footer="Modmail System v2.0 with Persistent Storage"
    )
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx):
    category = await get_or_create_category(ctx.guild)

    log_channel = discord.utils.get(ctx.guild.text_channels, name=LOG_CHANNEL_NAME)
    if not log_channel:
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=False),
            ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        staff_role = discord.utils.get(ctx.guild.roles, name=STAFF_ROLE_NAME)
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True)
        log_channel = await ctx.guild.create_text_channel(LOG_CHANNEL_NAME, overwrites=overwrites)

    fields = [
        ("Category", category.name, True),
        ("Log Channel", log_channel.mention, True),
        ("Staff Role", STAFF_ROLE_NAME, True),
        ("Prefix", "!", True),
        ("Data Directory", DATA_DIR, False),
        ("Auto-save Interval", f"{AUTO_SAVE_INTERVAL}s", True),
        ("Backup Interval", f"{BACKUP_INTERVAL}s ({BACKUP_INTERVAL // 3600}h)", True),
    ]
    embed = build_embed(
        "Modmail Setup Complete",
        "The modmail system is ready with persistent storage enabled.\nTranscripts will be automatically saved to disk.",
        color=discord.Color.green(),
        fields=fields
    )
    await ctx.send(embed=embed)
    log("info", f"[SETUP] {ctx.author} ran setup in {ctx.guild}")


@bot.command()
@staff_only()
async def opentickets(ctx):
    if not open_tickets:
        return await ctx.send(embed=build_embed("Open Tickets", "No tickets are currently open.", color=discord.Color.green()))

    lines = []
    for user_id, ticket in open_tickets.items():
        channel = bot.get_channel(ticket["channel_id"])
        claimed = claimed_tickets.get(user_id, "Unclaimed")
        channel_mention = channel.mention if channel else f"#ticket-{user_id}"
        lines.append(f"{channel_mention} — Claimed by: **{claimed}** — Messages: **{len(ticket_messages.get(user_id, []))}**")

    embed = build_embed(
        f"Open Tickets ({len(open_tickets)})",
        "\n".join(lines),
        color=discord.Color.blurple()
    )
    await ctx.send(embed=embed)


# ═══════════════════════════════════════════════════════════════════════════
# GRACEFUL SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════

async def shutdown():
    """Graceful shutdown handler."""
    log("info", "[SHUTDOWN] Initiating graceful shutdown...")
    
    # Stop background tasks
    auto_save_task.stop()
    backup_task.stop()
    
    # Final save
    log("info", "[SHUTDOWN] Performing final state save...")
    save_state()
    save_blacklist()
    
    # Create final backup
    create_backup()
    
    log("info", "[SHUTDOWN] Shutdown complete. All data saved.")


@bot.event
async def on_disconnect():
    log("warning", "[CONNECTION] Bot disconnected from Discord")


@bot.event
async def on_resumed():
    log("info", "[CONNECTION] Bot reconnected to Discord")


# ═══════════════════════════════════════════════════════════════════════════
# RUN BOT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        bot.run("YOUR_BOT_TOKEN_HERE")
    except KeyboardInterrupt:
        log("info", "[SHUTDOWN] Keyboard interrupt received")
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_until_complete(shutdown())
    except Exception as e:
        log("error", f"[FATAL] Bot crashed: {e}")
        save_state()
        save_blacklist()
