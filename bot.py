import discord
from discord.ext import commands
import json
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python 3.9+

# ==============================
# KONFIGURATION (Railway ENV)
# ==============================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])

AUTO_DELETE_SECONDS = 300
TZ = ZoneInfo("Europe/Berlin")  # Sommer-/Winterzeit wird automatisch berücksichtigt

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
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ==============================
# HILFSFUNKTIONEN
# ==============================
async def safe_delete_message(msg: discord.Message, label: str = ""):
    if msg is None:
        return
    try:
        await msg.delete()
    except discord.Forbidden as e:
        print(f"❌ Forbidden beim Löschen {label}: {e}", flush=True)
    except discord.NotFound:
        pass
    except Exception as e:
        print(f"❌ Fehler beim Löschen {label}: {type(e).__name__}: {e}", flush=True)

async def send_temp(ctx, content: str):
    return await ctx.send(content, delete_after=AUTO_DELETE_SECONDS)

def parse_reminder_to_minutes(token: str) -> int:
    token = token.strip().lower()
    if token.endswith("m"):
        return int(token[:-1])
    if token.endswith("h"):
        return int(token[:-1]) * 60
    if token.endswith("d"):
        return int(token[:-1]) * 1440
    raise ValueError("Reminder format invalid")

def parse_berlin_datetime(datum: str, uhrzeit: str) -> datetime:
    naive = datetime.strptime(f"{datum} {uhrzeit}", "%d-%m-%Y %H:%M")
    # Als lokale Berlin-Zeit interpretieren (ZoneInfo berechnet Offset inkl. DST)
    return naive.replace(tzinfo=TZ)

def parse_iso_any(dt_str: str) -> datetime:
    """
    Liest ISO-Zeit aus JSON.
    - Wenn Zeitzone fehlt (alte Daten), behandeln wir sie als Europe/Berlin.
    """
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt

# ==============================
# READY EVENT
# ==============================
@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)
    bot.loop.create_task(erinnerungs_task())
    print("⏰ Erinnerungs-Task gestartet", flush=True)

# ==============================
# COMMANDS
# ==============================
@bot.command()
async def ping(ctx):
    await send_temp(ctx, "🏓 Pong! Ich funktioniere.")
    await safe_delete_message(ctx.message, label="[user cmd ping]")

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
        terminzeit_berlin = parse_berlin_datetime(datum, uhrzeit)
    except Exception:
        await send_temp(ctx, "❌ Falsches Datum/Uhrzeit! Beispiel: `08-02-2026 12:00`")
        await safe_delete_message(ctx.message, label="[user cmd termin]")
        return

    termine = load_json("termine.json", [])
    termine.append({
        "titel": titel,
        "zeit": terminzeit_berlin.isoformat(),  # enthält Offset (z.B. +01:00 / +02:00)
        "erinnerung": minuten,
        "gesendet": False
    })
    save_json("termine.json", termine)

    await send_temp(
        ctx,
        f"📅 **Termin gespeichert!**\n"
        f"📌 {titel}\n"
        f"⏰ {datum} {uhrzeit} (Europe/Berlin)\n"
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
        zeit = parse_iso_any(t["zeit"]).astimezone(TZ)
        out += f"{i+1}. {t['titel']} – {zeit.strftime('%d.%m %H:%M')} (Berlin)\n"

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
# ERINNERUNGEN
# ==============================
async def erinnerungs_task():
    await bot.wait_until_ready()
    channel = bot.get_channel(ERINNERUNGS_CHANNEL_ID)

    if channel is None:
        print("❌ Erinnerungs-Channel nicht gefunden. Prüfe ERINNERUNGS_CHANNEL_ID!", flush=True)
        return

    print(f"✅ Erinnerungs-Channel gefunden: {channel.name} ({channel.id})", flush=True)

    while not bot.is_closed():
        try:
            jetzt = datetime.now(tz=TZ)  # Berlin-Zeit
            termine = load_json("termine.json", [])
            geändert = False

            for t in termine:
                if t.get("gesendet"):
                    continue

                terminzeit = parse_iso_any(t["zeit"]).astimezone(TZ)
                erinnerungszeit = terminzeit - timedelta(minutes=int(t["erinnerung"]))

                if jetzt >= erinnerungszeit:
                    await channel.send(
                        f"<@&{ROLLE_ID}> 🔔 **ERINNERUNG** 🔔\n"
                        f"📌 **{t['titel']}**\n"
                        f"⏰ Termin um {terminzeit.strftime('%H:%M')} (Berlin)",
                        delete_after=AUTO_DELETE_SECONDS
                    )
                    print(f"🔔 Erinnerung gesendet: {t['titel']} -> {terminzeit.isoformat()}", flush=True)

                    t["gesendet"] = True
                    geändert = True

            if geändert:
                save_json("termine.json", termine)

        except Exception as e:
            print(f"❌ Fehler im Erinnerungs-Loop: {type(e).__name__}: {e}", flush=True)

        await asyncio.sleep(30)  # öfter prüfen, damit Tests schneller sind

# ==============================
# START
# ==============================
bot.run(BOT_TOKEN)
