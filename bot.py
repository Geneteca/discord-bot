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

# Nachrichten nach X Sekunden löschen (5 Minuten)
AUTO_DELETE_SECONDS = 300

# ==============================
# BOT SETUP
# ==============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================
# JSON DATEI FUNKTIONEN
# ==============================
def load_json(filename, default):
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=2)

# ==============================
# HILFSFUNKTIONEN (DEBUG-SAFE)
# ==============================
async def safe_delete_message(msg: discord.Message, label: str = ""):
    """Versucht eine Nachricht zu löschen und loggt Fehler in Railway."""
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception as e:
        print(f"❌ Konnte Nachricht nicht löschen {label}: {type(e).__name__}: {e}")

async def send_temp(ctx, content: str):
    """Sendet eine Nachricht und löscht sie nach AUTO_DELETE_SECONDS."""
    msg = await ctx.send(content)
    try:
        await asyncio.sleep(AUTO_DELETE_SECONDS)
        await safe_delete_message(msg, label="[bot reply]")
    except Exception as e:
        print(f"❌ Fehler beim Auto-Delete der Bot-Nachricht: {type(e).__name__}: {e}")

# ==============================
# READY EVENT
# ==============================
@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}")
    try:
        bot.loop.create_task(erinnerungs_task())
        print("⏰ Erinnerungs-Task gestartet")
    except Exception as e:
        print(f"❌ Fehler beim Starten des Erinnerungs-Tasks: {type(e).__name__}: {e}")

# ==============================
# TEST COMMAND
# ==============================
@bot.command()
async def ping(ctx):
    await send_temp(ctx, "🏓 Pong! Ich funktioniere.")
    await safe_delete_message(ctx.message, label="[user cmd ping]")

# ==============================
# TODO SYSTEM
# ==============================
@bot.command()
async def todo(ctx, *, text):
    todos = load_json("todos.json", [])
    todos.append({"text": text, "done": False})
    save_json("todos.json", todos)

    await send_temp(ctx, f"✅ To-Do hinzugefügt: **{text}**")
    await safe_delete_message(ctx.message, label="[user cmd todo]")

@bot.command()
async def todos(ctx):
    todos = load_json("todos.json", [])
    if not todos:
        await send_temp(ctx, "🎉 Keine To-Dos vorhanden!")
        await safe_delete_message(ctx.message, label="[user cmd todos]")
        return

    out = "**📝 To-Do-Liste:**\n"
    for i, t in enumerate(todos):
        status = "✅" if t.get("done") else "❌"
        out += f"{i+1}. {status} {t.get('text', '')}\n"

    await send_temp(ctx, out)
    await safe_delete_message(ctx.message, label="[user cmd todos]")

@bot.command()
async def done(ctx, nummer: int):
    todos = load_json("todos.json", [])
    if 1 <= nummer <= len(todos):
        todos[nummer - 1]["done"] = True
        save_json("todos.json", todos)
        await send_temp(ctx, "🎉 To-Do erledigt!")
    else:
        await send_temp(ctx, "❌ Ungültige Nummer")

    await safe_delete_message(ctx.message, label="[user cmd done]")

# ==============================
# TERMIN SYSTEM (DD-MM-YYYY)
# ==============================
def parse_reminder_to_minutes(token: str):
    token = token.strip().lower()
    if token.endswith("m"):
        return int(token[:-1])
    if token.endswith("h"):
        return int(token[:-1]) * 60
    if token.endswith("d"):
        return int(token[:-1]) * 1440
    raise ValueError("Reminder format invalid")

@bot.command()
async def termin(ctx, datum, uhrzeit, *, rest):
    teile = rest.split()
    if len(teile) < 2:
        await send_temp(ctx, "❌ Format: `!termin 08-02-2026 12:00 PD Meeting 30m`")
        await safe_delete_message(ctx.message, label="[user cmd termin]")
        return

    titel = " ".join(teile[:-1])
    erinnerung = teile[-1]

    try:
        minuten = parse_reminder_to_minutes(erinnerung)
    except Exception:
        await send_temp(ctx, "❌ Erinnerung z. B. `10m`, `1h` oder `1d`")
        await safe_delete_message(ctx.message, label="[user cmd termin]")
        return

    try:
        terminzeit = datetime.strptime(f"{datum} {uhrzeit}", "%d-%m-%Y %H:%M")
    except Exception:
        await send_temp(ctx, "❌ Falsches Datum/Uhrzeit! Beispiel: `08-02-2026 12:00`")
        await safe_delete_message(ctx.message, label="[user cmd termin]")
        return

    termine = load_json("termine.json", [])
    termine.append({
        "titel": titel,
        "zeit": terminzeit.isoformat(),
        "erinnerung": minuten,
        "gesendet": False
    })
    save_json("termine.json", termine)

    await send_temp(
        ctx,
        f"📅 **Termin gespeichert!**\n"
        f"📌 {titel}\n"
        f"⏰ {datum} {uhrzeit}\n"
        f"🔔 {minuten} Minuten vorher"
    )
    await safe_delete_message(ctx.message, label="[user cmd termin]")

@bot.command()
async def termine(ctx):
    termine = load_json("termine.json", [])
    if not termine:
        await send_temp(ctx, "📭 Keine Termine gespeichert.")
        await safe_delete_message(ctx.message, label="[user cmd termine]")
        return

    out = "**📅 Termine:**\n"
    for i, t in enumerate(termine):
        zeit = datetime.fromisoformat(t["zeit"])
        out += f"{i+1}. {t['titel']} – {zeit.strftime('%d.%m %H:%M')}\n"

    await send_temp(ctx, out)
    await safe_delete_message(ctx.message, label="[user cmd termine]")

@bot.command()
async def absagen(ctx, nummer: int):
    termine = load_json("termine.json", [])
    if 1 <= nummer <= len(termine):
        t = termine.pop(nummer - 1)
        save_json("termine.json", termine)
        await send_temp(ctx, f"❌ Termin **{t['titel']}** abgesagt!")
    else:
        await send_temp(ctx, "❌ Ungültige Nummer")

    await safe_delete_message(ctx.message, label="[user cmd absagen]")

# ==============================
# ERINNERUNGEN (ROLLE PING + AUTO-DELETE)
# ==============================
async def erinnerungs_task():
    await bot.wait_until_ready()
    channel = bot.get_channel(ERINNERUNGS_CHANNEL_ID)

    if channel is None:
        print("❌ Erinnerungs-Channel nicht gefunden. Prüfe ERINNERUNGS_CHANNEL_ID!")
        return

    print(f"✅ Erinnerungs-Channel gefunden: {channel.name} ({channel.id})")

    while not bot.is_closed():
        try:
            jetzt = datetime.now()
            termine = load_json("termine.json", [])
            geändert = False

            for t in termine:
                if t.get("gesendet"):
                    continue

                terminzeit = datetime.fromisoformat(t["zeit"])
                erinnerungszeit = terminzeit - timedelta(minutes=int(t["erinnerung"]))

                if jetzt >= erinnerungszeit:
                    try:
                        msg = await channel.send(
                            f"<@&{ROLLE_ID}> 🔔 **ERINNERUNG** 🔔\n"
                            f"📌 **{t['titel']}**\n"
                            f"⏰ Termin um {terminzeit.strftime('%H:%M')}"
                        )
                        # nach 5 Minuten löschen
                        await asyncio.sleep(AUTO_DELETE_SECONDS)
                        await safe_delete_message(msg, label="[reminder msg]")
                    except Exception as e:
                        print(f"❌ Fehler beim Senden/Löschen der Erinnerung: {type(e).__name__}: {e}")

                    t["gesendet"] = True
                    geändert = True

            if geändert:
                save_json("termine.json", termine)

        except Exception as e:
            print(f"❌ Fehler im Erinnerungs-Loop: {type(e).__name__}: {e}")

        await asyncio.sleep(60)

# ==============================
# BOT STARTEN
# ==============================
bot.run(BOT_TOKEN)

