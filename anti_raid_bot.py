# anti_raid_bot_debug.py
import os
import discord
from discord.ext import commands
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta

# ------------- CONFIG din variabile de mediu -------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# join flood config
JOIN_THRESHOLD = int(os.getenv("JOIN_THRESHOLD", "6"))
JOIN_WINDOW_SECONDS = int(os.getenv("JOIN_WINDOW_SECONDS", "20"))

# account age (in days) considered "new" and suspicious
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "7"))
AUTO_BAN_NEW_ACCOUNTS = os.getenv("AUTO_BAN_NEW_ACCOUNTS", "false").lower() == "true"

# spam config (messages per user in window)
SPAM_MSG_THRESHOLD = int(os.getenv("SPAM_MSG_THRESHOLD", "12"))
SPAM_WINDOW_SECONDS = int(os.getenv("SPAM_WINDOW_SECONDS", "8"))

# lockdown auto-restore (seconds)
AUTO_UNLOCK_SECONDS = int(os.getenv("AUTO_UNLOCK_SECONDS", "300"))

# ------------- Intents & Bot -------------
intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.messages = True  # pentru on_message spam
# intents.message_content = True  # doar dacă vrei comenzi prefix (!)

bot = commands.Bot(command_prefix="!", intents=intents)

# ------------- Runtime state -------------
recent_joins = defaultdict(lambda: deque())
user_msgs = defaultdict(lambda: deque())
guild_lock_state = {}

# top spam tracker
spam_counter = defaultdict(int)

# helper: get log channel
def get_log_channel(guild: discord.Guild):
    if LOG_CHANNEL_ID:
        ch = guild.get_channel(LOG_CHANNEL_ID)
        return ch
    return None

async def log(guild: discord.Guild, message: str):
    ch = get_log_channel(guild)
    try:
        if ch:
            await ch.send(f"[ANTI-RAID] {message}")
        print(f"[ANTI-RAID][{guild.name if guild else 'Unknown'}] {message}")
    except Exception as e:
        print("Log failed:", e)

# ------------- Member join handler -------------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    now = datetime.now(timezone.utc)

    dq = recent_joins[guild.id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > JOIN_WINDOW_SECONDS:
        dq.popleft()

    await log(guild, f"Member joined: {member} ({member.created_at.date()}) — {len(dq)} joins in last {JOIN_WINDOW_SECONDS}s")

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

    # join flood
    if len(dq) >= JOIN_THRESHOLD:
        await log(guild, f"Join threshold reached: {len(dq)} joins in {JOIN_WINDOW_SECONDS}s. Triggering lockdown.")
        await trigger_lockdown(guild)

# ------------- Message spam detection -------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    guild = message.guild
    if not guild:
        return

    now = datetime.now(timezone.utc)
    dq = user_msgs[message.author.id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > SPAM_WINDOW_SECONDS:
        dq.popleft()

    # debug print
    print(f"Mesaj de la {message.author}: {message.content} ({len(dq)} in window)")

    # update spam counter
    spam_counter[message.author.id] = len(dq)

    if len(dq) >= SPAM_MSG_THRESHOLD:
        await log(guild, f"User {message.author} suspected spam ({len(dq)} msgs in {SPAM_WINDOW_SECONDS}s). Applying action.")
        try:
            if hasattr(message.author, "timeout"):
                await message.author.timeout(duration=600, reason="Spam detected by anti-raid")
                await log(guild, f"Timed out {message.author} for spam.")
            else:
                await message.author.kick(reason="Spam detected by anti-raid")
                await log(guild, f"Kicked {message.author} for spam.")
        except Exception as e:
            await log(guild, f"Failed action on {message.author}: {e}")

        user_msgs[message.author.id].clear()

    await bot.process_commands(message)

# command to see top spam users
@bot.command(name="topspam")
@commands.has_permissions(administrator=True)
async def cmd_topspam(ctx):
    top = sorted(spam_counter.items(), key=lambda x: x[1], reverse=True)[:5]
    msg = "Top spam users:\n"
    for uid, count in top:
        member = ctx.guild.get_member(uid)
        if member:
            msg += f"{member} → {count} msgs\n"
    await ctx.send(msg)

# ------------- Lockdown / Unlock -------------
async def trigger_lockdown(guild: discord.Guild):
    if guild.id in guild_lock_state:
        await log(guild, "Guild already locked.")
        return

    prev_overwrites = {}
    changed_channels = []
    for ch in guild.text_channels:
        try:
            prev = ch.overwrites_for(guild.default_role)
            prev_overwrites[ch.id] = prev
            await ch.set_permissions(guild.default_role, send_messages=False, reason="Auto lockdown")
            changed_channels.append(ch.id)
        except Exception as e:
            await log(guild, f"Failed to change channel {ch.name}: {e}")

    guild_lock_state[guild.id] = {'channels': prev_overwrites, 'locked_at': datetime.now(timezone.utc)}
    await log(guild, f"Lockdown enabled on {len(changed_channels)} channels.")

    if AUTO_UNLOCK_SECONDS > 0:
        await schedule_unlock(guild, AUTO_UNLOCK_SECONDS)

async def schedule_unlock(guild: discord.Guild, delay_seconds: int):
    await log(guild, f"Auto-unlock scheduled in {delay_seconds}s.")
    await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
    await unlock_guild(guild)

async def unlock_guild(guild: discord.Guild):
    state = guild_lock_state.get(guild.id)
    if not state:
        await log(guild, "Guild not locked.")
        return

    prev_overwrites = state['channels']
    restored = 0
    for ch_id, prev in prev_overwrites.items():
        ch = guild.get_channel(ch_id)
        if ch:
            try:
                await ch.set_permissions(guild.default_role, overwrite=prev, reason="Auto unlock")
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
        await ctx.send(f"Server is in lockdown since {state['locked_at'].isoformat()}")
    else:
        await ctx.send("Server is not locked.")

# ------------- Startup check -------------
@bot.event
async def on_ready():
    print(f"Anti-raid bot conectat ca {bot.user} (guilds: {len(bot.guilds)})")
    recent_joins.clear()
    user_msgs.clear()
    guild_lock_state.clear()
    for g in bot.guilds:
        await log(g, "Anti-raid bot este online.")

# ------------- Run -------------
if not DISCORD_TOKEN:
    print("DISCORD_TOKEN nu este setat. Seteaza variabila de mediu DISCORD_TOKEN.")
else:
    bot.run(DISCORD_TOKEN)
