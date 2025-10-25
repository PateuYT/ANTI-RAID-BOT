# anti_raid_youtube_bot.py
import os
import discord
from discord.ext import commands, tasks
from collections import deque, defaultdict
from datetime import datetime, timezone, timedelta
import aiohttp

# -------- CONFIG din variabile de mediu --------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Anti-raid config
JOIN_THRESHOLD = int(os.getenv("JOIN_THRESHOLD", "6"))
JOIN_WINDOW_SECONDS = int(os.getenv("JOIN_WINDOW_SECONDS", "20"))
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "7"))
AUTO_BAN_NEW_ACCOUNTS = os.getenv("AUTO_BAN_NEW_ACCOUNTS", "false").lower() == "true"
SPAM_MSG_THRESHOLD = int(os.getenv("SPAM_MSG_THRESHOLD", "12"))
SPAM_WINDOW_SECONDS = int(os.getenv("SPAM_WINDOW_SECONDS", "8"))
AUTO_UNLOCK_SECONDS = int(os.getenv("AUTO_UNLOCK_SECONDS", "300"))

# YouTube config
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")

# -------- Intents & Bot --------
intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)

# -------- Runtime state --------
recent_joins = defaultdict(lambda: deque())
user_msgs = defaultdict(lambda: deque())
spam_counter = defaultdict(int)
guild_lock_state = {}
last_video_id = None

# -------- Helper: log channel --------
def get_log_channel(guild: discord.Guild):
    return guild.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None

async def log(guild: discord.Guild, message: str):
    ch = get_log_channel(guild)
    if ch:
        try:
            await ch.send(f"[ANTI-RAID] {message}")
        except Exception as e:
            print(f"[DEBUG] Nu s-a putut trimite mesaj: {e}")
    print(f"[ANTI-RAID][{guild.name if guild else 'Unknown'}] {message}")

# -------- Member join handler --------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    now = datetime.now(timezone.utc)
    dq = recent_joins[guild.id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > JOIN_WINDOW_SECONDS:
        dq.popleft()
    await log(guild, f"Member joined: {member} — {len(dq)} joins in last {JOIN_WINDOW_SECONDS}s")

    # cont nou suspect
    age = now - member.created_at
    if age < timedelta(days=MIN_ACCOUNT_AGE_DAYS):
        await log(guild, f"Member {member} are cont prea nou: {age.days}d")
        if AUTO_BAN_NEW_ACCOUNTS:
            try:
                await member.ban(reason="Account too new — possible raid bot")
                await log(guild, f"Banned {member} automat.")
            except Exception as e:
                await log(guild, f"Nu s-a putut ban {member}: {e}")

    # join flood
    if len(dq) >= JOIN_THRESHOLD:
        await log(guild, f"Join threshold atins: {len(dq)} joins în {JOIN_WINDOW_SECONDS}s. Lockdown!")
        await trigger_lockdown(guild)

# -------- Message spam detection --------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild = message.guild
    now = datetime.now(timezone.utc)
    dq = user_msgs[message.author.id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > SPAM_WINDOW_SECONDS:
        dq.popleft()

    spam_counter[message.author.id] = len(dq)

    if len(dq) >= SPAM_MSG_THRESHOLD:
        await log(guild, f"User {message.author} suspected spam ({len(dq)} msgs in {SPAM_WINDOW_SECONDS}s).")
        try:
            # Timeout 10 minute
            until_time = datetime.now(timezone.utc) + timedelta(seconds=600)
            await message.author.timeout(until=until_time, reason="Spam detectat de bot")
            await log(guild, f"Timed out {message.author} pentru spam.")
        except Exception as e_timeout:
            try:
                await message.author.kick(reason="Spam detectat de bot")
                await log(guild, f"Kicked {message.author} pentru spam (timeout nereușit).")
            except Exception as e_kick:
                await log(guild, f"Nu s-a putut aplica acțiune pe {message.author}: {e_timeout} / {e_kick}")
        user_msgs[message.author.id].clear()

    await bot.process_commands(message)

# -------- Top spam command --------
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

# -------- Lockdown / Unlock --------
async def trigger_lockdown(guild: discord.Guild):
    if guild.id in guild_lock_state:
        await log(guild, "Server deja în lockdown.")
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
            await log(guild, f"Nu s-a putut schimba canalul {ch.name}: {e}")

    guild_lock_state[guild.id] = {'channels': prev_overwrites, 'locked_at': datetime.now(timezone.utc)}
    await log(guild, f"Lockdown activat pe {len(changed_channels)} canale.")

    if AUTO_UNLOCK_SECONDS > 0:
        bot.loop.create_task(schedule_unlock(guild, AUTO_UNLOCK_SECONDS))

async def schedule_unlock(guild: discord.Guild, delay_seconds: int):
    await discord.utils.sleep_until(datetime.now(timezone.utc) + timedelta(seconds=delay_seconds))
    await unlock_guild(guild)

async def unlock_guild(guild: discord.Guild):
    state = guild_lock_state.get(guild.id)
    if not state:
        await log(guild, "Serverul nu e în lockdown.")
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
                await log(guild, f"Nu s-a putut restaura canalul {ch.name}: {e}")

    del guild_lock_state[guild.id]
    await log(guild, f"Lockdown ridicat. {restored} canale restaurate.")

# -------- Admin commands --------
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
        await ctx.send(f"Server este în lockdown din {state['locked_at'].isoformat()}")
    else:
        await ctx.send("Serverul nu este în lockdown.")

# -------- YouTube checker --------
async def check_youtube():
    global last_video_id
    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
        return

    url = f"https://www.googleapis.com/youtube/v3/search?key={YOUTUBE_API_KEY}&channelId={YOUTUBE_CHANNEL_ID}&part=snippet,id&order=date&maxResults=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            items = data.get("items", [])
            if not items:
                return
            video = items[0]
            video_id = video['id'].get('videoId')
            if not video_id or video_id == last_video_id:
                return
            last_video_id = video_id
            link = f"https://youtu.be/{video_id}"
            # trimite mesaj in fiecare guild
            for guild in bot.guilds:
                await log(guild, f"@everyone P4TEU a postat un videoclip nou! {link}")

@tasks.loop(seconds=60)
async def youtube_task():
    await check_youtube()

# -------- On Ready --------
@bot.event
async def on_ready():
    print(f"[DEBUG] Anti-raid bot conectat ca {bot.user} (guilds: {len(bot.guilds)})")
    recent_joins.clear()
    user_msgs.clear()
    guild_lock_state.clear()
    youtube_task.start()
    for g in bot.guilds:
        await log(g, "Anti-raid bot este online.")

# -------- Run --------
if not DISCORD_TOKEN:
    print("⚠ DISCORD_TOKEN nu este setat!")
else:
    bot.run(DISCORD_TOKEN)
