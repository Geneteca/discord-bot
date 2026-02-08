import discord
from discord.ext import commands
import json
import os
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

# ==============================
# KONFIGURATION (Railway ENV)
# ==============================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])

# ✅ 15 Minuten Auto-Löschung
AUTO_DELETE_SECONDS = 900

TZ = ZoneInfo("Europe/Berlin")
REMINDER_TOKEN_RE = re.compile(r"^\d+(m|h|d)$", re.IGNORECASE)

# ==============================
# BOT SETUP
# ==============================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ==============================
# JSON HELPERS
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
# TIME PARSING
# ==============================
def parse_dt(datum: str, uhrzeit: str) -> datetime:
    """
    Akzeptiert:
      - DD.MM.YYYY HH:MM (neu)
      - DD-MM-YYYY HH:MM (alt)
    """
    s = f"{datum} {uhrzeit}"
    for fmt in ("%d.%m.%Y %H:%M", "%d-%m-%Y %H:%M"):
        try:
            naive = datetime.strptime(s, fmt)
            return naive.replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError("Ungültiges Datum/Uhrzeit Format")

def dt_from_iso_any(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)

# ==============================
# SAFE DELETE
# ==============================
async def safe_delete_message(msg: discord.Message, label: str = ""):
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception as e:
        print(f"❌ Konnte Nachricht nicht löschen {label}: {type(e).__name__}: {e}", flush=True)

# ==============================
# REMINDER PARSING
# ==============================
def reminder_token_to_minutes(token: str) -> int:
    token = token.strip().lower()
    if token.endswith("m"):
        return int(token[:-1])
    if token.endswith("h"):
        return int(token[:-1]) * 60
    if token.endswith("d"):
        return int(token[:-1]) * 1440
    raise ValueError("Reminder-Format ungültig")

def split_title_and_reminders(rest: str):
    """
    Erwartet: "<titel...> <rem1> <rem2> ..."
    Beispiel: "PD Meeting 60m 10m 5m"
    """
    tokens = rest.split()
    if not tokens:
        raise ValueError("Rest leer")

    reminder_tokens = []
    while tokens and REMINDER_TOKEN_RE.match(tokens[-1]):
        reminder_tokens.append(tokens.pop())

    if not reminder_tokens:
        raise ValueError("Keine Reminder-Tokens gefunden (z.B. 30m)")

    titel = " ".join(tokens).strip()
    if not titel:
        raise ValueError("Titel fehlt")

    minutes = [reminder_token_to_minutes(t) for t in reminder_tokens]
    minutes = sorted(set(minutes), reverse=True)  # unique + absteigend
    return titel, minutes

def remove_mention_tokens_from_title(title: str) -> str:
    # entfernt grob Mention-Tokens aus Titel
    return re.sub(r"<@!?&?\d+>", "", title).strip()

# ==============================
# DATA MIGRATION (alte Termine)
# ==============================
def migrate_termine(termine: list) -> bool:
    """
    Migriert alte Struktur:
      - erinnerung_min -> erinnerungen_min (list)
      - gesendet bool -> gesendet_min (list)
      - abgeschlossen default False
    """
    changed = False
    for t in termine:
        if "abgeschlossen" not in t:
            t["abgeschlossen"] = False
            changed = True

        if "erinnerung_min" in t and "erinnerungen_min" not in t:
            try:
                m = int(t["erinnerung_min"])
            except Exception:
                m = 0
            t["erinnerungen_min"] = [m] if m > 0 else []
            changed = True

        if isinstance(t.get("gesendet"), bool):
            if "gesendet_min" not in t:
                t["gesendet_min"] = list(t.get("erinnerungen_min", [])) if t["gesendet"] else []
                changed = True

        if "gesendet_min" not in t:
            t["gesendet_min"] = []
            changed = True

    return changed

# ==============================
# READY
# ==============================
@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)
    bot.loop.create_task(erinnerungs_loop())

# ==============================
# BASIC
# ==============================
@bot.command()
async def ping(ctx):
    await ctx.send("🏓 Pong! Ich funktioniere.", delete_after=AUTO_DELETE_SECONDS)
    await safe_delete_message(ctx.message, "[user cmd ping]")

# ==============================
# TODO SYSTEM
# ==============================
@bot.command()
async def todo(ctx, *, text):
    todos = load_json("todos.json", [])
    todos.append({"text": text, "done": False})
    save_json("todos.json", todos)

    await ctx.send(f"✅ To-Do hinzugefügt: **{text}**", delete_after=AUTO_DELETE_SECONDS)
    await safe_delete_message(ctx.message, "[user cmd todo]")

@bot.command()
async def todos(ctx):
    todos = load_json("todos.json", [])
    if not todos:
        await ctx.send("🎉 Keine To-Dos vorhanden!", delete_after=AUTO_DELETE_SECONDS)
        await safe_delete_message(ctx.message, "[user cmd todos]")
        return

    out = "**📝 To-Do-Liste:**\n"
    for i, t in enumerate(todos):
        status = "✅" if t.get("done") else "❌"
        out += f"{i+1}. {status} {t.get('text','')}\n"

    await ctx.send(out, delete_after=AUTO_DELETE_SECONDS)
    await safe_delete_message(ctx.message, "[user cmd todos]")

@bot.command()
async def done(ctx, nummer: int):
    todos = load_json("todos.json", [])
    if 1 <= nummer <= len(todos):
        todos[nummer-1]["done"] = True
        save_json("todos.json", todos)
        await ctx.send("🎉 To-Do erledigt!", delete_after=AUTO_DELETE_SECONDS)
    else:
        await ctx.send("❌ Ungültige Nummer", delete_after=AUTO_DELETE_SECONDS)

    await safe_delete_message(ctx.message, "[user cmd done]")

# ==============================
# TERMINE (öffentlich im Channel) - ROLLE PING
# Beispiel:
# !termin 08.02.2026 12:00 PD Meeting 60m 10m 5m
# ==============================
@bot.command()
async def termin(ctx, datum, uhrzeit, *, rest):
    try:
        titel, reminders_min = split_title_and_reminders(rest)
    except Exception:
        await ctx.send(
            "❌ Format: `!termin 08.02.2026 12:00 PD Meeting 60m 10m 5m`",
            delete_after=AUTO_DELETE_SECONDS
        )
        await safe_delete_message(ctx.message, "[user cmd termin]")
        return

    try:
        dt = parse_dt(datum, uhrzeit)
    except Exception:
        await ctx.send("❌ Datum/Uhrzeit falsch. Beispiel: `08.02.2026 12:00`", delete_after=AUTO_DELETE_SECONDS)
        await safe_delete_message(ctx.message, "[user cmd termin]")
        return

    termine = load_json("termine.json", [])
    termine.append({
        "titel": titel,
        "zeit": dt.isoformat(),
        "erinnerungen_min": reminders_min,
        "gesendet_min": [],
        "abgeschlossen": False,
        "typ": "channel"
    })
    save_json("termine.json", termine)

    rem_txt = ", ".join(f"{m}m" for m in reminders_min)

    # ✅ Rolle wird bei !termin gepingt
    await ctx.send(
        f"<@&{ROLLE_ID}> 📅 **Termin gespeichert!**\n"
        f"📌 {titel}\n"
        f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n"
        f"🔔 **Erinnerung:** {rem_txt} vorher",
        delete_after=AUTO_DELETE_SECONDS
    )
    await safe_delete_message(ctx.message, "[user cmd termin]")

# ==============================
# PRIVATE TERMINE (DM) - KEIN ROLLEN PING
# Beispiel:
# !ptermin 08.02.2026 12:00 Arzt 60m 10m @Person2 @Person3
# ==============================
@bot.command()
async def ptermin(ctx, datum, uhrzeit, *, rest):
    try:
        titel, reminders_min = split_title_and_reminders(rest)
    except Exception:
        await ctx.send(
            "❌ Format: `!ptermin 08.02.2026 12:00 Titel 60m 10m @Person`",
            delete_after=AUTO_DELETE_SECONDS
        )
        await safe_delete_message(ctx.message, "[user cmd ptermin]")
        return

    titel = remove_mention_tokens_from_title(titel)

    try:
        dt = parse_dt(datum, uhrzeit)
    except Exception:
        await ctx.send("❌ Datum/Uhrzeit falsch. Beispiel: `08.02.2026 12:00`", delete_after=AUTO_DELETE_SECONDS)
        await safe_delete_message(ctx.message, "[user cmd ptermin]")
        return

    user_ids = {ctx.author.id}
    for m in ctx.message.mentions:
        user_ids.add(m.id)

    termine = load_json("termine.json", [])
    termine.append({
        "titel": titel,
        "zeit": dt.isoformat(),
        "erinnerungen_min": reminders_min,
        "gesendet_min": [],
        "abgeschlossen": False,
        "typ": "dm",
        "user_ids": sorted(list(user_ids))
    })
    save_json("termine.json", termine)

    rem_txt = ", ".join(f"{m}m" for m in reminders_min)

    # ✅ kein Rollen-Ping
    await ctx.send(
        f"📩 **Privater Termin gespeichert!**\n"
        f"📌 {titel}\n"
        f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n"
        f"🔔 **Erinnerung:** {rem_txt} vorher\n"
        f"👥 Empfänger: {len(user_ids)}",
        delete_after=AUTO_DELETE_SECONDS
    )
    await safe_delete_message(ctx.message, "[user cmd ptermin]")

# ==============================
# TERMINE: aktive / alle
# ==============================
@bot.command()
async def termine(ctx):
    termine = load_json("termine.json", [])
    changed = migrate_termine(termine)
    aktive = [t for t in termine if not t.get("abgeschlossen", False)]
    if changed:
        save_json("termine.json", termine)

    if not aktive:
        await ctx.send("📭 Keine aktiven Termine gespeichert.", delete_after=AUTO_DELETE_SECONDS)
        await safe_delete_message(ctx.message, "[user cmd termine]")
        return

    out = "**📅 Aktive Termine:**\n"
    for i, t in enumerate(aktive, start=1):
        dt = dt_from_iso_any(t["zeit"])
        typ = "DM" if t.get("typ") == "dm" else "Channel"
        rems = ", ".join(f"{m}m" for m in t.get("erinnerungen_min", [])) or "—"
        out += f"{i}. [{typ}] {t.get('titel','')} – {dt.strftime('%d.%m.%Y %H:%M')} – Erinnerung: {rems}\n"

    await ctx.send(out, delete_after=AUTO_DELETE_SECONDS)
    await safe_delete_message(ctx.message, "[user cmd termine]")

@bot.command()
async def termine_all(ctx):
    termine = load_json("termine.json", [])
    changed = migrate_termine(termine)
    if changed:
        save_json("termine.json", termine)

    if not termine:
        await ctx.send("📭 Keine Termine gespeichert.", delete_after=AUTO_DELETE_SECONDS)
        await safe_delete_message(ctx.message, "[user cmd termine_all]")
        return

    out = "**📅 Alle Termine:**\n"
    for i, t in enumerate(termine, start=1):
        dt = dt_from_iso_any(t["zeit"])
        typ = "DM" if t.get("typ") == "dm" else "Channel"
        status = "✅ abgeschlossen" if t.get("abgeschlossen") else "🟡 aktiv"
        rems = ", ".join(f"{m}m" for m in t.get("erinnerungen_min", [])) or "—"
        out += f"{i}. [{typ}] {t.get('titel','')} – {dt.strftime('%d.%m.%Y %H:%M')} – Erinnerung: {rems} – {status}\n"

    await ctx.send(out, delete_after=AUTO_DELETE_SECONDS)
    await safe_delete_message(ctx.message, "[user cmd termine_all]")

@bot.command()
async def absagen(ctx, nummer: int):
    termine = load_json("termine.json", [])
    changed = migrate_termine(termine)

    if 1 <= nummer <= len(termine):
        t = termine.pop(nummer - 1)
        save_json("termine.json", termine)
        await ctx.send(f"❌ Termin abgesagt: **{t.get('titel','')}**", delete_after=AUTO_DELETE_SECONDS)
    else:
        if changed:
            save_json("termine.json", termine)
        await ctx.send("❌ Ungültige Nummer", delete_after=AUTO_DELETE_SECONDS)

    await safe_delete_message(ctx.message, "[user cmd absagen]")

# ==============================
# ERINNERUNGS LOOP (mehrere Erinnerungen)
# ==============================
async def erinnerungs_loop():
    await bot.wait_until_ready()

    channel = bot.get_channel(ERINNERUNGS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(ERINNERUNGS_CHANNEL_ID)
        except Exception as e:
            print(f"❌ Erinnerungs-Channel nicht gefunden: {e}", flush=True)
            return

    print(f"✅ Erinnerungs-Channel gefunden: {channel.name} ({channel.id})", flush=True)

    while not bot.is_closed():
        try:
            jetzt = datetime.now(tz=TZ)
            termine = load_json("termine.json", [])
            changed = migrate_termine(termine)
            any_change = changed

            for t in termine:
                if t.get("abgeschlossen", False):
                    continue

                dt = dt_from_iso_any(t["zeit"])

                if jetzt >= dt:
                    t["abgeschlossen"] = True
                    any_change = True
                    continue

                titel = t.get("titel", "Termin")
                when = dt.strftime("%d.%m.%Y %H:%M")
                reminders = t.get("erinnerungen_min", [])
                sent = set(int(x) for x in t.get("gesendet_min", []))

                for m in reminders:
                    m = int(m)
                    if m in sent:
                        continue

                    erinnerungszeit = dt - timedelta(minutes=m)
                    if jetzt >= erinnerungszeit:
                        if t.get("typ") == "dm":
                            # ✅ DM: kein Rollen-Ping
                            for uid in t.get("user_ids", []):
                                try:
                                    user = bot.get_user(uid) or await bot.fetch_user(uid)
                                    if user:
                                        await user.send(
                                            f"🔔 **Erinnerung** ({m} min vorher)\n"
                                            f"📌 **{titel}**\n"
                                            f"🕒 {when} (Berlin)"
                                        )
                                except Exception as e:
                                    print(f"❌ DM fehlgeschlagen an {uid}: {e}", flush=True)
                        else:
                            # ✅ Channel: Rolle pingen
                            await channel.send(
                                f"<@&{ROLLE_ID}> 🔔 **Erinnerung** ({m} min vorher)\n"
                                f"📌 **{titel}**\n"
                                f"🕒 {when} (Berlin)",
                                delete_after=AUTO_DELETE_SECONDS
                            )

                        sent.add(m)
                        t["gesendet_min"] = sorted(list(sent))
                        any_change = True

            if any_change:
                save_json("termine.json", termine)

        except Exception as e:
            print(f"❌ Fehler im Erinnerungs-Loop: {type(e).__name__}: {e}", flush=True)

        await asyncio.sleep(30)

# ==============================
# START
# ==============================
bot.run(BOT_TOKEN)
