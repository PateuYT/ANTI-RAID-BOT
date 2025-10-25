# anti_raid_bot.py
import os
import discord
from discord.ext import tasks, commands
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

# ------------- CONFIG din variabile de mediu -------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# join flood config
JOIN_THRESHOLD = int(os.getenv("JOIN_THRESHOLD", "6"))          # ex: 6 joins
JOIN_WINDOW_SECONDS = int(os.getenv("JOIN_WINDOW_SECONDS", "20"))  # ex: 20s

# account age (in days) considered "new" and suspicious
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "7"))
AUTO_BAN_NEW_ACCOUNTS = os.getenv("AUTO_BAN_NEW_ACCOUNTS", "false").lower() == "true"

# spam config (messages per user in window)
SPAM_MSG_THRESHOLD = int(os.getenv("SPAM_MSG_THRESHOLD", "12"))
SPAM_WINDOW_SECONDS = int(os.getenv("SPAM_WINDOW_SECONDS", "8"))

# lockdown auto-restore (seconds) - optional
AUTO_UNLOCK_SECONDS = int(os.getenv("AUTO_UNLOCK_SECONDS", "300"))  # 5 minutes by default, 0 = manual

# ------------- Intents & Bot -------------
intents = discord.Intents.default()
intents.members = True  # privileged intent — activează în Developer Portal
intents.guilds = True
intents.messages = True
# intents.message_content = True  # only if you need to analyze message text content

bot = commands.Bot(command_prefix="!", intents=intents)

# ------------- Runtime state -------------
# track recent joins per guild: deque of timestamps
recent_joins = defaultdict(lambda: deque())

# track recent message timestamps per user (for spam)
user_msgs = defaultdict(lambda: deque())

# store which guilds are locked and the previous overwrites we changed (to restore)
guild_lock_state = {}  # guild_id -> { 'channels': {channel_id: prev_overwrite}, 'locked_at': datetime }

# helper: get log channel
def get_log_channel(guild: discord.Guild):
    if LOG_CHANNEL_ID:
        ch = guild.get_channel(LOG_CHANNEL_ID) or bot.get_channel(LOG_CHANNEL_ID)
        return ch
    return None

async def log(guild: discord.Guild, message: str):
    ch = get_log_channel(guild)
    try:
        if ch:
            await ch.send(f"[ANTI-RAID] {message}")
        else:
            print(f"[ANTI-RAID][Guild {guild.id}] {message}")
    except Exception as e:
        print("Log failed:", e)

# ------------- Member join handler -------------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    now = datetime.now(timezone.utc)

    # record join
    dq = recent_joins[guild.id]
    dq.append(now)
    # remove old timestamps
    while dq and (now - dq[0]).total_seconds() > JOIN_WINDOW_SECONDS:
        dq.popleft()

    await log(guild, f"Member joined: {member} (acct created: {member.created_at.isoformat()}) — {len(dq)} joins in last {JOIN_WINDOW_SECONDS}s")

    # check account age
    age = now - member.created_at
    if age < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
        await log(guild, f"Member {member} has account age {age.days}d < {MIN_ACCOUNT_AGE_DAYS}d (suspicious).")
        if AUTO_BAN_NEW_ACCOUNTS:
            try:
                await member.ban(reason="Account too new — possible raid bot")
                await log(guild, f"Banned new account {member} automatically.")
            except Exception as e:
                await log(guild, f"Failed to ban {member}: {e}")

    # if join flood threshold exceeded -> lockdown
    if len(dq) >= JOIN_THRESHOLD:
        await log(guild, f"Join threshold reached: {len(dq)} joins in {JOIN_WINDOW_SECONDS}s. Triggering lockdown.")
        await trigger_lockdown(guild)

# ------------- Message spam detection -------------
@bot.event
async def on_message(message: discord.Message):
    # ignore bot messages
    if message.author.bot:
        return

    guild = message.guild
    if guild is None:
        return

    now = datetime.now(timezone.utc)
    dq = user_msgs[message.author.id]
    dq.append(now)
    # cleanup
    while dq and (now - dq[0]).total_seconds() > SPAM_WINDOW_SECONDS:
        dq.popleft()

    # if user exceeds messages threshold in window -> action
    if len(dq) >= SPAM_MSG_THRESHOLD:
        await log(guild, f"User {message.author} suspected spam ({len(dq)} msgs in {SPAM_WINDOW_SECONDS}s). Applying timeout/kick.")
        try:
            # try timeout (requires Moderate Members perm)
            if hasattr(message.author, "timeout"):
                # discord.py v2.4+ supports Member.timeout; we try to timeout 10 minutes
                await message.author.timeout(duration=600, reason="Spam detected by anti-raid")
                await log(guild, f"Timed out {message.author} for spam.")
            else:
                # fallback: kick
                await message.author.kick(reason="Spam detected by anti-raid")
                await log(guild, f"Kicked {message.author} for spam.")
        except Exception as e:
            await log(guild, f"Failed action on {message.author}: {e}")

        # clear user's message history to avoid repeat
        user_msgs[message.author.id].clear()

    # allow commands to still work if you use commands
    await bot.process_commands(message)

# ------------- Lockdown / Unlock functions -------------
async def trigger_lockdown(guild: discord.Guild):
    if guild.id in guild_lock_state:
        await log(guild, "Guild already locked.")
        return

    prev_overwrites = {}
    changed_channels = []
    for ch in guild.text_channels:
        try:
            # save previous overwrite for @everyone
            prev = ch.overwrites_for(guild.default_role)
            prev_overwrites[ch.id] = prev
            # set send_messages False for @everyone
            await ch.set_permissions(guild.default_role, send_messages=False, reason="Auto lockdown by anti-raid bot")
            changed_channels.append(ch.id)
        except Exception as e:
            await log(guild, f"Failed to change channel {ch.name}: {e}")

    guild_lock_state[guild.id] = {
        'channels': prev_overwrites,
        'locked_at': datetime.now(timezone.utc)
    }

    await log(guild, f"Lockdown enabled on {len(changed_channels)} channels.")

    # if auto-unlock configured
    if AUTO_UNLOCK_SECONDS and AUTO_UNLOCK_SECONDS > 0:
        await schedule_unlock(guild, AUTO_UNLOCK_SECONDS)

async def schedule_unlock(guild: discord.Guild, delay_seconds: int):
    await log(guild, f"Auto-unlock scheduled in {delay_seconds}s.")
    await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
    await unlock_guild(guild)

async def unlock_guild(guild: discord.Guild):
    state = guild_lock_state.get(guild.id)
    if not state:
        await log(guild, "Guild is not locked.")
        return

    prev_overwrites = state.get('channels', {})
    restored = 0
    for ch_id, prev in prev_overwrites.items():
        ch = guild.get_channel(ch_id)
        if ch:
            try:
                await ch.set_permissions(guild.default_role, overwrite=prev, reason="Auto unlock by anti-raid bot")
                restored += 1
            except Exception as e:
                await log(guild, f"Failed to restore channel {ch.name}: {e}")

    del guild_lock_state[guild.id]
    await log(guild, f"Lockdown lifted. Restored {restored} channels.")

# ------------- Admin commands -------------
@bot.command(name="antiraid-lock")
@commands.has_permissions(administrator=True)
async def cmd_lock(ctx):
    await ctx.send("Activating lockdown...")
    await trigger_lockdown(ctx.guild)

@bot.command(name="antiraid-unlock")
@commands.has_permissions(administrator=True)
async def cmd_unlock(ctx):
    await ctx.send("Lifting lockdown...")
    await unlock_guild(ctx.guild)

@bot.command(name="antiraid-status")
@commands.has_permissions(administrator=True)
async def cmd_status(ctx):
    state = guild_lock_state.get(ctx.guild.id)
    if state:
        locked_at = state.get('locked_at')
        await ctx.send(f"Server is in lockdown since {locked_at.isoformat()}.")
    else:
        await ctx.send("Server is not locked.")

# ------------- Startup check -------------
@bot.event
async def on_ready():
    print(f"Anti-raid bot conectat ca {bot.user} (guilds: {len(bot.guilds)})")
    # optional: clear runtime state
    recent_joins.clear()
    user_msgs.clear()
    guild_lock_state.clear()
    # log to default servers
    for g in bot.guilds:
        await log(g, "Anti-raid bot este online.")

# ------------- Run -------------
if not DISCORD_TOKEN:
    print("DISCORD_TOKEN nu este setat. Seteaza variabila de mediu DISCORD_TOKEN.")
else:
    bot.run(DISCORD_TOKEN)
