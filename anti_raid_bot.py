# -------- Message spam detection corect --------
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

    # șterge mesajele mai vechi de SPAM_WINDOW_SECONDS
    while dq and (now - dq[0]).total_seconds() > SPAM_WINDOW_SECONDS:
        dq.popleft()

    spam_counter[message.author.id] = len(dq)

    # dacă depășește pragul de spam
    if len(dq) >= SPAM_MSG_THRESHOLD:
        await log(guild, f"User {message.author} suspected spam ({len(dq)} msgs in {SPAM_WINDOW_SECONDS}s).")

        try:
            # Timeout (10 minute)
            until_time = datetime.now(timezone.utc) + timedelta(seconds=600)
            await message.author.timeout(until=until_time, reason="Spam detectat de bot")
            await log(guild, f"Timed out {message.author} pentru spam.")
        except Exception as e_timeout:
            # Dacă nu merge timeout → kick
            try:
                await message.author.kick(reason="Spam detectat de bot")
                await log(guild, f"Kicked {message.author} pentru spam (timeout nereușit).")
            except Exception as e_kick:
                await log(guild, f"Nu s-a putut aplica acțiune pe {message.author}: {e_timeout} / {e_kick}")

        # resetează contorul pentru user
        user_msgs[message.author.id].clear()

    # procesare comenzi dacă există
    await bot.process_commands(message)
