import os
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List

import discord
from discord import app_commands

# =========================
# ENV / KONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])  # Standard-Channel für /termin
TZ = ZoneInfo("Europe/Berlin")

# Optional: damit Slash-Commands sofort auf deinem Server erscheinen:
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None

DATA_FILE = "data.json"
CHECK_INTERVAL_SECONDS = 20  # wie oft geprüft wird

# =========================
# DATENMODELL
# =========================
def _now_berlin() -> datetime:
    return datetime.now(tz=TZ)

def _parse_dt_berlin(date_str: str, time_str: str) -> datetime:
    # date: DD-MM-YYYY, time: HH:MM
    naive = datetime.strptime(f"{date_str} {time_str}", "%d-%m-%Y %H:%M")
    return naive.replace(tzinfo=TZ)

def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()

def _dt_from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)

def _add_months(dt: datetime, months: int) -> datetime:
    # simple month increment without extra libs
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    day = dt.day

    # clamp day to last day of target month
    # (handle Feb etc.)
    # compute last day by going to first of next month minus one day
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=dt.tzinfo)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day

    day = min(day, last_day)
    return dt.replace(year=year, month=month, day=day)

def _next_occurrence(dt: datetime, recurrence: str) -> datetime:
    # recurrence: "none", "daily", "weekly", "monthly"
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(weeks=1)
    if recurrence == "monthly":
        return _add_months(dt, 1)
    return dt

def _normalize_reminders(reminders: List[int]) -> List[int]:
    # minutes BEFORE event, unique, sorted desc? we prefer ascending for readability
    rem = sorted(set(int(x) for x in reminders if int(x) >= 0))
    return rem

def load_data() -> dict:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"events": [], "next_id": 1}

def save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def new_event_id(data: dict) -> int:
    eid = int(data.get("next_id", 1))
    data["next_id"] = eid + 1
    return eid

# =========================
# DISCORD CLIENT
# =========================
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # für User-Auswahl in Slash-Commands sehr hilfreich
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync: sofort in einer Guild, wenn GUILD_ID gesetzt ist, sonst global
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"✅ Slash-Commands synced to guild {GUILD_ID}", flush=True)
        else:
            await self.tree.sync()
            print("✅ Slash-Commands global synced (kann bis ~1h dauern)", flush=True)

bot = MyBot()

# =========================
# BACKGROUND CHECKER
# =========================
async def send_to_targets(
    *,
    client: discord.Client,
    event: dict,
    msg_text: str
) -> None:
    target_type = event["target"]["type"]  # "channel" oder "dm"
    if target_type == "channel":
        channel_id = int(event["target"]["channel_id"])
        channel = client.get_channel(channel_id)
        if channel is None:
            # try fetch
            try:
                channel = await client.fetch_channel(channel_id)
            except Exception as e:
                print(f"❌ Channel not found/fetch failed: {channel_id} {e}", flush=True)
                return

        await channel.send(msg_text)
        return

    # DM
    user_ids = [int(x) for x in event["target"].get("user_ids", [])]
    for uid in user_ids:
        try:
            user = client.get_user(uid) or await client.fetch_user(uid)
            if user:
                await user.send(msg_text)
        except Exception as e:
            print(f"❌ DM failed to {uid}: {e}", flush=True)

def build_reminder_message(event: dict, minutes_before: int) -> str:
    title = event["title"]
    dt = _dt_from_iso(event["datetime"])
    # optional role mention in channel only? user wants DM or selected persons for private;
    # for channel variant: we keep it clean without role ping unless you want again.
    # If you still want role ping, we can add ROLE_ID as env and mention it here.
    when = dt.strftime("%d.%m.%Y %H:%M")
    return (
        f"🔔 **Erinnerung** ({minutes_before} min vorher)\n"
        f"📌 **{title}**\n"
        f"🕒 {when} (Berlin)"
    )

def build_created_message(event: dict) -> str:
    dt = _dt_from_iso(event["datetime"])
    when = dt.strftime("%d.%m.%Y %H:%M")
    rems = ", ".join(f"{m}m" for m in event["reminders_minutes"]) if event["reminders_minutes"] else "—"
    rec = event["recurrence"]
    rec_txt = {"none":"einmalig", "daily":"täglich", "weekly":"wöchentlich", "monthly":"monatlich"}.get(rec, rec)
    return (
        f"✅ Termin gespeichert (ID **{event['id']}**)\n"
        f"📌 **{event['title']}**\n"
        f"🕒 {when} (Berlin)\n"
        f"⏰ Erinnerungen: {rems}\n"
        f"🔁 Wiederholung: {rec_txt}"
    )

async def reminder_loop():
    await bot.wait_until_ready()
    print("⏰ Reminder-Loop läuft", flush=True)

    while not bot.is_closed():
        try:
            data = load_data()
            events = data.get("events", [])
            now = _now_berlin()

            changed = False

            for ev in events:
                if ev.get("cancelled"):
                    continue

                dt = _dt_from_iso(ev["datetime"])
                # Ensure tracking set
                ev.setdefault("sent_reminders", [])  # list of minutes already sent for current occurrence

                # send reminders
                for m in ev.get("reminders_minutes", []):
                    remind_at = dt - timedelta(minutes=int(m))
                    if now >= remind_at and int(m) not in ev["sent_reminders"] and now < dt + timedelta(hours=24):
                        msg = build_reminder_message(ev, int(m))
                        await send_to_targets(client=bot, event=ev, msg_text=msg)
                        ev["sent_reminders"].append(int(m))
                        changed = True

                # if event time passed: handle recurrence or mark done
                if now >= dt:
                    rec = ev.get("recurrence", "none")
                    if rec and rec != "none":
                        next_dt = _next_occurrence(dt, rec)
                        ev["datetime"] = _dt_to_iso(next_dt)
                        ev["sent_reminders"] = []
                        changed = True
                    else:
                        # one-time: mark done
                        ev["cancelled"] = True
                        changed = True

            if changed:
                save_data(data)

        except Exception as e:
            print(f"❌ Fehler im Reminder-Loop: {type(e).__name__}: {e}", flush=True)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# =========================
# SLASH COMMANDS
# =========================

def parse_reminders_string(s: str) -> List[int]:
    """
    Erwartet z.B.: "30,10,5" => [30,10,5]
    oder leer => []
    """
    s = (s or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return _normalize_reminders([int(p) for p in parts])

@app_commands.command(name="termin", description="Termin im festen Channel ankündigen (öffentlich)")
@app_commands.describe(
    datum="DD-MM-YYYY (z.B. 08-02-2026)",
    uhrzeit="HH:MM (z.B. 12:00)",
    titel="Titel des Termins",
    erinnerungen="Minuten vorher, Komma-getrennt (z.B. 30,10) – leer = keine",
    wiederholung="none/daily/weekly/monthly"
)
async def termin_cmd(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerungen: str = "30",
    wiederholung: str = "none"
):
    await interaction.response.defer(ephemeral=True)

    try:
        dt = _parse_dt_berlin(datum, uhrzeit)
    except Exception:
        await interaction.followup.send("❌ Datum/Uhrzeit falsch. Format: `08-02-2026` und `12:00`", ephemeral=True)
        return

    rec = wiederholung.lower().strip()
    if rec not in ("none", "daily", "weekly", "monthly"):
        await interaction.followup.send("❌ wiederholung muss sein: none/daily/weekly/monthly", ephemeral=True)
        return

    reminders = parse_reminders_string(erinnerungen)

    data = load_data()
    ev_id = new_event_id(data)
    event = {
        "id": ev_id,
        "title": titel,
        "datetime": _dt_to_iso(dt),
        "reminders_minutes": reminders,
        "sent_reminders": [],
        "recurrence": rec,
        "cancelled": False,
        "target": {
            "type": "channel",
            "channel_id": ERINNERUNGS_CHANNEL_ID
        }
    }
    data["events"].append(event)
    save_data(data)

    # Bestätigung (ephemeral)
    await interaction.followup.send(build_created_message(event), ephemeral=True)

@app_commands.command(name="ptermin", description="Privater Termin: Erinnerung per DM an dich oder ausgewählte Personen")
@app_commands.describe(
    datum="DD-MM-YYYY (z.B. 08-02-2026)",
    uhrzeit="HH:MM (z.B. 12:00)",
    titel="Titel des Termins",
    erinnerungen="Minuten vorher, Komma-getrennt (z.B. 30,10) – leer = keine",
    wiederholung="none/daily/weekly/monthly",
    person1="Optional: weitere Person",
    person2="Optional: weitere Person",
    person3="Optional: weitere Person",
    person4="Optional: weitere Person",
    person5="Optional: weitere Person"
)
async def ptermin_cmd(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerungen: str = "30",
    wiederholung: str = "none",
    person1: Optional[discord.Member] = None,
    person2: Optional[discord.Member] = None,
    person3: Optional[discord.Member] = None,
    person4: Optional[discord.Member] = None,
    person5: Optional[discord.Member] = None
):
    await interaction.response.defer(ephemeral=True)

    try:
        dt = _parse_dt_berlin(datum, uhrzeit)
    except Exception:
        await interaction.followup.send("❌ Datum/Uhrzeit falsch. Format: `08-02-2026` und `12:00`", ephemeral=True)
        return

    rec = wiederholung.lower().strip()
    if rec not in ("none", "daily", "weekly", "monthly"):
        await interaction.followup.send("❌ wiederholung muss sein: none/daily/weekly/monthly", ephemeral=True)
        return

    reminders = parse_reminders_string(erinnerungen)

    # Empfänger: standardmäßig du selbst + optional markierte
    user_ids = {interaction.user.id}
    for p in (person1, person2, person3, person4, person5):
        if p is not None:
            user_ids.add(p.id)

    data = load_data()
    ev_id = new_event_id(data)
    event = {
        "id": ev_id,
        "title": titel,
        "datetime": _dt_to_iso(dt),
        "reminders_minutes": reminders,
        "sent_reminders": [],
        "recurrence": rec,
        "cancelled": False,
        "target": {
            "type": "dm",
            "user_ids": sorted(list(user_ids))
        }
    }
    data["events"].append(event)
    save_data(data)

    await interaction.followup.send(build_created_message(event) + "\n📩 Versand: DM", ephemeral=True)

@app_commands.command(name="termine", description="Zeigt kommende Termine (mit IDs) an")
async def termine_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    events = [e for e in data.get("events", []) if not e.get("cancelled")]

    now = _now_berlin()
    # sort by date
    events.sort(key=lambda e: _dt_from_iso(e["datetime"]))

    if not events:
        await interaction.followup.send("📭 Keine Termine gespeichert.", ephemeral=True)
        return

    lines = []
    for e in events[:25]:
        dt = _dt_from_iso(e["datetime"])
        if dt < now - timedelta(days=1):
            continue
        rec = e.get("recurrence", "none")
        rec_txt = {"none":"einmalig", "daily":"täglich", "weekly":"wöchentlich", "monthly":"monatlich"}.get(rec, rec)
        rems = ",".join(str(m) for m in e.get("reminders_minutes", [])) or "—"
        target = e["target"]["type"]
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {rec_txt} · {target}")

    out = "\n".join(lines) if lines else "📭 Keine (aktuellen) Termine."
    await interaction.followup.send(out, ephemeral=True)

@app_commands.command(name="termin_absagen", description="Löscht/stoppt einen Termin (per ID)")
@app_commands.describe(termin_id="ID aus /termine")
async def termin_absagen_cmd(interaction: discord.Interaction, termin_id: int):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    events = data.get("events", [])
    found = False
    for e in events:
        if int(e.get("id", -1)) == int(termin_id) and not e.get("cancelled"):
            e["cancelled"] = True
            found = True
            break

    if found:
        save_data(data)
        await interaction.followup.send(f"❌ Termin **{termin_id}** abgesagt.", ephemeral=True)
    else:
        await interaction.followup.send("❌ ID nicht gefunden oder schon abgesagt.", ephemeral=True)

@app_commands.command(name="termin_edit", description="Bearbeitet einen Termin (per ID)")
@app_commands.describe(
    termin_id="ID aus /termine",
    datum="Optional: DD-MM-YYYY",
    uhrzeit="Optional: HH:MM",
    titel="Optional: neuer Titel",
    erinnerungen="Optional: z.B. 60,10",
    wiederholung="Optional: none/daily/weekly/monthly"
)
async def termin_edit_cmd(
    interaction: discord.Interaction,
    termin_id: int,
    datum: Optional[str] = None,
    uhrzeit: Optional[str] = None,
    titel: Optional[str] = None,
    erinnerungen: Optional[str] = None,
    wiederholung: Optional[str] = None
):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    events = data.get("events", [])
    ev = None
    for e in events:
        if int(e.get("id", -1)) == int(termin_id) and not e.get("cancelled"):
            ev = e
            break

    if ev is None:
        await interaction.followup.send("❌ Termin-ID nicht gefunden.", ephemeral=True)
        return

    # update title
    if titel is not None and titel.strip():
        ev["title"] = titel.strip()

    # update recurrence
    if wiederholung is not None:
        rec = wiederholung.lower().strip()
        if rec not in ("none", "daily", "weekly", "monthly"):
            await interaction.followup.send("❌ wiederholung muss sein: none/daily/weekly/monthly", ephemeral=True)
            return
        ev["recurrence"] = rec

    # update reminders
    if erinnerungen is not None:
        ev["reminders_minutes"] = parse_reminders_string(erinnerungen)
        ev["sent_reminders"] = []  # reset for safety

    # update date/time (need both if one is missing we use existing)
    if datum is not None or uhrzeit is not None:
        current_dt = _dt_from_iso(ev["datetime"])
        d = datum if datum is not None else current_dt.strftime("%d-%m-%Y")
        t = uhrzeit if uhrzeit is not None else current_dt.strftime("%H:%M")
        try:
            new_dt = _parse_dt_berlin(d, t)
        except Exception:
            await interaction.followup.send("❌ Neues Datum/Uhrzeit ungültig.", ephemeral=True)
            return
        ev["datetime"] = _dt_to_iso(new_dt)
        ev["sent_reminders"] = []

    save_data(data)
    await interaction.followup.send("✅ Termin aktualisiert:\n" + build_created_message(ev), ephemeral=True)

# Register commands
bot.tree.add_command(termin_cmd)
bot.tree.add_command(ptermin_cmd)
bot.tree.add_command(termine_cmd)
bot.tree.add_command(termin_absagen_cmd)
bot.tree.add_command(termin_edit_cmd)

# =========================
# STARTUP
# =========================
@bot.event
async def on_connect():
    print("🔌 Connected", flush=True)

@bot.event
async def on_disconnect():
    print("🔌 Disconnected", flush=True)

async def main():
    async with bot:
        bot.loop.create_task(reminder_loop())
        await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.clear_commands(guild=None)  # ❗ löscht globale Commands
    await bot.tree.sync()
    print("🧹 Globale Slash-Commands gelöscht")
