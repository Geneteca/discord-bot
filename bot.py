import discord
from discord.ext import commands
import json
import asyncio
import os
from datetime import datetime, timedelta

# ==============================
# KONFIGURATION (Railway ENV)
# ==============================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])

# ==============================
# BOT SETUP
# ==============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================
# HILFSFUNKTIONEN
# ==============================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return default

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)

# ==============================
# READY EVENT
# ==============================
@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}")
    bot.loop.create_task(erinnerungs_task())

# ==============================
# TEST COMMAND
# ==============================
@bot.command()
async def ping(ctx):
    msg = await ctx.send("🏓 Pong! Ich funktioniere.")
    await asyncio.sleep(300)
    await msg.delete()
    await ctx.message.delete()

# ==============================
# TODO SYSTEM
# ==============================
@bot.command()
async def todo(ctx, *, text):
    todos = load_json("todos.json", [])
    todos.append({"text": text, "done": False})
    save_json("todos.json", todos)
    msg = await ctx.send(f"✅ To-Do hinzugefügt: **{text}**")
    await asyncio.sleep(300)
    await msg.delete()
    await ctx.message.delete()

@bot.command()
async def todos(ctx):
    todos = load_json("todos.json", [])
    if not todos:
        msg = await ctx.send("🎉 Keine To-Dos vorhanden!")
        await asyncio.sleep(300)
        await msg.delete()
        await ctx.message.delete()
        return

    text = "**📝 To-Do-Liste:**\n"
    for i, t in enumerate(todos):
        status = "✅" if t["done"] else "❌"
        text += f"{i+1}. {status} {t['text']}\n"

    msg = await ctx.send(text)
    await asyncio.sleep(300)
    await msg.delete()
    await ctx.message.delete()

@bot.command()
async def done(ctx, nummer: int):
    todos = load_json("todos.json", [])
    if 1 <= nummer <= len(todos):
        todos[nummer-1]["done"] = True
        save_json("todos.json", todos)
        msg = await ctx.send("🎉 To-Do erledigt!")
    else:
        msg = await ctx.send("❌ Ungültige Nummer")

    await asyncio.sleep(300)
    await msg.delete()
    await ctx.message.delete()

# ==============================
# TERMIN SYSTEM (DD-MM-YYYY)
# ==============================
@bot.command()
async def termin(ctx, datum, uhrzeit, *, rest):
    teile = rest.split()
    titel = " ".join(teile[:-1])
    erinnerung = teile[-1]

    if erinnerung.endswith("m"):
        minuten = int(erinnerung[:-1])
    elif erinnerung.endswith("h"):
        minuten = int(erinnerung[:-1]) * 60
    elif erinnerung.endswith("d"):
        minuten = int(erinnerung[:-1]) * 1440
    else:
        msg = await ctx.send("❌ Erinnerung z. B. 10m, 1h oder 1d")
        await asyncio.sleep(300)
        await msg.delete()
        await ctx.message.delete()
        return

    try:
        terminzeit = datetime.strptime(f"{datum} {uhrzeit}", "%d-%m-%Y %H:%M")
    except:
        msg = await ctx.send("❌ Falsches Datum! Beispiel: 08-02-2026 12:00")
        await asyncio.sleep(300)
        await msg.delete()
        await ctx.message.delete()
        return

    termine = load_json("termine.json", [])
    termine.append({
        "titel": titel,
        "zeit": terminzeit.isoformat(),
        "erinnerung": minuten,
        "gesendet": False
    })
    save_json("termine.json", termine)

    msg = await ctx.send(
        f"📅 **Termin gespeichert!**\n"
        f"📌 {titel}\n"
        f"⏰ {datum} {uhrzeit}\n"
        f"🔔 {minuten} Minuten vorher"
    )
    await asyncio.sleep(300)
    await msg.delete()
    await ctx.message.delete()

@bot.command()
async def absagen(ctx, nummer: int):
    termine = load_json("termine.json", [])
    if 1 <= nummer <= len(termine):
        t = termine.pop(nummer-1)
        save_json("termine.json", termine)
        msg = await ctx.send(f"❌ Termin **{t['titel']}** abgesagt!")
    else:
        msg = await ctx.send("❌ Ungültige Nummer")

    await asyncio.sleep(300)
    await msg.delete()
    await ctx.message.delete()

# ==============================
# ERINNERUNGEN
# ==============================
async def erinnerungs_task():
    await bot.wait_until_ready()
    channel = bot.get_channel(ERINNERUNGS_CHANNEL_ID)

    while not bot.is_closed():
        jetzt = datetime.now()
        termine = load_json("termine.json", [])
        geändert = False

        for t in termine:
            if t["gesendet"]:
                continue

            terminzeit = datetime.fromisoformat(t["zeit"])
            erinnerungszeit = terminzeit - timedelta(minutes=t["erinnerung"])

            if jetzt >= erinnerungszeit:
                msg = await channel.send(
                    f"<@&{ROLLE_ID}> 🔔 **ERINNERUNG** 🔔\n"
                    f"📌 **{t['titel']}**\n"
                    f"⏰ Termin um {terminzeit.strftime('%H:%M')}"
                )
                await asyncio.sleep(300)
                await msg.delete()
                t["gesendet"] = True
                geändert = True

        if geändert:
            save_json("termine.json", termine)

        await asyncio.sleep(60)

# ==============================
# BOT STARTEN
# ==============================
bot.run(BOT_TOKEN)
