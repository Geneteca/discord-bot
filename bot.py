import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import discord
from discord import app_commands

# =========================
# ENV / KONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])

# Optional: Alte globale Slash-Commands einmalig löschen
CLEAN_GLOBAL_COMMANDS = os.environ.get("CLEAN_GLOBAL_COMMANDS", "0").strip() == "1"

TZ = ZoneInfo("Europe/Berlin")
DATA_FILE = "data.json"

AUTO_DELETE_SECONDS = 900  # ✅ 15 Minuten
CHECK_INTERVAL_SECONDS = 20

# =========================
# Daten / Helpers
# =========================
def now_berlin() -> datetime:
    return datetime.now(tz=TZ)

def dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()

def dt_from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)

def parse_date_time(date_str: str, time_str: str) -> datetime:
    """
    Erwartet:
      date_str: DD.MM.YYYY
      time_str: HH:MM
    """
    naive = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return naive.replace(tzinfo=TZ)

def parse_reminders(rem_str: str) -> List[int]:
    """
    Erlaubt:
      "60,10,5"  -> [60,10,5]
      "60m,10m"  -> [60,10]
      "1h,10m"   -> [60,10]
      "1d"       -> [1440]
    """
    rem_str = (rem_str or "").strip()
    if not rem_str:
        return []

    parts = [p.strip().lower() for p in rem_str.split(",") if p.strip()]
    out: List[int] = []
    for p in parts:
        if p.endswith("m"):
            out.append(int(p[:-1]))
        elif p.endswith("h"):
            out.append(int(p[:-1]) * 60)
        elif p.endswith("d"):
            out.append(int(p[:-1]) * 1440)
        else:
            # falls jemand "30" schreibt
            out.append(int(p))
    # unique, sort absteigend (60,10,5)
    out = sorted(set(x for x in out if x >= 0), reverse=True)
    return out

def load_data() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "events" not in data:
            data["events"] = []
        if "next_id" not in data:
            data["next_id"] = 1
        return data
    except Exception:
        return {"events": [], "next_id": 1}

def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def new_id(data: Dict[str, Any]) -> int:
    nid = int(data.get("next_id", 1))
    data["next_id"] = nid + 1
    return nid

def add_month(dt: datetime) -> datetime:
    # Monats-Addition ohne externe Libs, Tag wird ggf. geklemmt
    y = dt.year
    m = dt.month + 1
    if m == 13:
        m = 1
        y += 1

    # Letzter Tag im Zielmonat ermitteln
    if m == 12:
        next_month = datetime(y + 1, 1, 1, tzinfo=dt.tzinfo)
    else:
        next_month = datetime(y, m + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day

    d = min(dt.day, last_day)
    return dt.replace(year=y, month=m, day=d)

def next_occurrence(dt: datetime, recurrence: str) -> datetime:
    recurrence = (recurrence or "none").lower()
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(weeks=1)
    if recurrence == "monthly":
        return add_month(dt)
    return dt

# =========================
# Discord Client (Slash only)
# =========================
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # damit Member-Auswahl für /ptermin klappt
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        # Commands werden NUR in dieser Guild synchronisiert (keine globalen)
        # Optional: alte globale Commands einmal löschen
        if CLEAN_GLOBAL_COMMANDS:
            print("🧹 CLEAN_GLOBAL_COMMANDS aktiv: Lösche alte globale Slash-Commands …", flush=True)
            # sichere lokale Commands
            saved_cmds = list(self.tree.get_commands())
            self.tree.clear_commands(guild=None)
            await self.tree.sync()  # global sync => entfernt globale remote commands
            # re-add local commands
            for c in saved_cmds:
                self.tree.add_command(c)
            print("✅ Globale Slash-Commands gelöscht.", flush=True)

        await self.tree.sync(guild=guild)
        print(f"✅ Slash-Commands synced to guild {GUILD_ID}", flush=True)

bot = MyBot()

# =========================
# Hintergrund-Reminder
# =========================
async def send_channel_message(channel_id: int, content: str):
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    await channel.send(content, delete_after=AUTO_DELETE_SECONDS)

async def send_dm(user_id: int, content: str):
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    await user.send(content)  # DMs nicht auto-löschbar zuverlässig

def reminder_text(title: str, dt: datetime, minutes_before: int) -> str:
    when = dt.strftime("%d.%m.%Y %H:%M")
    return (
        f"🔔 **Erinnerung** ({minutes_before} min vorher)\n"
        f"📌 **{title}**\n"
        f"🕒 {when} (Berlin)"
    )

async def reminder_loop():
    await bot.wait_until_ready()
    print("⏰ Reminder-Loop aktiv", flush=True)

    while not bot.is_closed():
        try:
            data = load_data()
            changed = False
            now = now_berlin()

            for ev in data["events"]:
                if ev.get("cancelled", False):
                    continue

                dt = dt_from_iso(ev["datetime"])
                reminders: List[int] = ev.get("reminders", [])
                sent: List[int] = ev.get("sent", [])
                sent_set = set(int(x) for x in sent)

                # Erinnerungen senden
                for m in reminders:
                    m = int(m)
                    if m in sent_set:
                        continue
                    if now >= (dt - timedelta(minutes=m)) and now < dt + timedelta(hours=24):
                        text = reminder_text(ev["title"], dt, m)

                        if ev["target"]["type"] == "channel":
                            # ✅ Rolle pingen im Channel
                            role_ping = f"<@&{ROLLE_ID}> "
                            await send_channel_message(ev["target"]["channel_id"], role_ping + text)
                        else:
                            # ✅ DM an alle Empfänger - KEIN Ping in DM
                            for uid in ev["target"]["user_ids"]:
                                await send_dm(uid, text)

                        sent_set.add(m)
                        ev["sent"] = sorted(list(sent_set), reverse=True)
                        changed = True

                # Terminzeit vorbei -> wiederkehrend oder abschließen
                if now >= dt:
                    rec = (ev.get("recurrence") or "none").lower()
                    if rec != "none":
                        nxt = next_occurrence(dt, rec)
                        ev["datetime"] = dt_to_iso(nxt)
                        ev["sent"] = []
                        changed = True
                    else:
                        ev["cancelled"] = True
                        changed = True

            if changed:
                save_data(data)

        except Exception as e:
            print(f"❌ Fehler im Reminder-Loop: {type(e).__name__}: {e}", flush=True)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# =========================
# Slash Commands
# =========================
REC_CHOICES = [
    app_commands.Choice(name="none", value="none"),
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="monthly", value="monthly"),
]

@bot.tree.command(name="termin", description="Öffentlicher Termin (Channel) mit Rollen-Ping")
@app_commands.describe(
    datum="DD.MM.YYYY (z.B. 08.02.2026)",
    uhrzeit="HH:MM (z.B. 12:00)",
    titel="Titel des Termins",
    erinnerung="Mehrere Erinnerungen: z.B. 60,10,5 (Minuten) oder 1h,10m",
    wiederholung="none/daily/weekly/monthly"
)
@app_commands.choices(wiederholung=REC_CHOICES)
async def termin_cmd(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerung: str = "30",
    wiederholung: app_commands.Choice[str] = REC_CHOICES[0],
):
    await interaction.response.defer(ephemeral=True)

    try:
        dt = parse_date_time(datum, uhrzeit)
    except Exception:
        await interaction.followup.send("❌ Datum/Uhrzeit ungültig. Beispiel: 08.02.2026 und 12:00", ephemeral=True)
        return

    reminders = parse_reminders(erinnerung)
    rec = wiederholung.value if wiederholung else "none"

    data = load_data()
    eid = new_id(data)

    ev = {
        "id": eid,
        "title": titel,
        "datetime": dt_to_iso(dt),
        "reminders": reminders,
        "sent": [],
        "recurrence": rec,
        "cancelled": False,
        "target": {"type": "channel", "channel_id": ERINNERUNGS_CHANNEL_ID},
        "created_by": interaction.user.id,
    }
    data["events"].append(ev)
    save_data(data)

    # ✅ Öffentliche Ankündigung im Channel inkl. Rollen-Ping (und Auto-Delete nach 15min)
    rem_txt = ", ".join(f"{m}m" for m in reminders) if reminders else "—"
    announce = (
        f"<@&{ROLLE_ID}> 📅 **Neuer Termin**\n"
        f"📌 **{titel}**\n"
        f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n"
        f"🔔 **Erinnerung:** {rem_txt} vorher\n"
        f"🆔 ID: **{eid}**"
    )
    await send_channel_message(ERINNERUNGS_CHANNEL_ID, announce)

    await interaction.followup.send(f"✅ Termin gespeichert. ID: **{eid}**", ephemeral=True)

@bot.tree.command(name="ptermin", description="Privater Termin per DM (an dich + ausgewählte Personen, ohne Ping in DM)")
@app_commands.describe(
    datum="DD.MM.YYYY (z.B. 08.02.2026)",
    uhrzeit="HH:MM (z.B. 12:00)",
    titel="Titel des Termins",
    erinnerung="Mehrere Erinnerungen: z.B. 60,10,5 (Minuten) oder 1h,10m",
    wiederholung="none/daily/weekly/monthly",
    person1="Optional",
    person2="Optional",
    person3="Optional",
    person4="Optional",
    person5="Optional",
)
@app_commands.choices(wiederholung=REC_CHOICES)
async def ptermin_cmd(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerung: str = "30",
    wiederholung: app_commands.Choice[str] = REC_CHOICES[0],
    person1: Optional[discord.Member] = None,
    person2: Optional[discord.Member] = None,
    person3: Optional[discord.Member] = None,
    person4: Optional[discord.Member] = None,
    person5: Optional[discord.Member] = None,
):
    await interaction.response.defer(ephemeral=True)

    try:
        dt = parse_date_time(datum, uhrzeit)
    except Exception:
        await interaction.followup.send("❌ Datum/Uhrzeit ungültig. Beispiel: 08.02.2026 und 12:00", ephemeral=True)
        return

    reminders = parse_reminders(erinnerung)
    rec = wiederholung.value if wiederholung else "none"

    # ✅ Empfänger: du + ausgewählte Personen
    ids = {interaction.user.id}
    for p in (person1, person2, person3, person4, person5):
        if p:
            ids.add(p.id)

    data = load_data()
    eid = new_id(data)

    ev = {
        "id": eid,
        "title": titel,
        "datetime": dt_to_iso(dt),
        "reminders": reminders,
        "sent": [],
        "recurrence": rec,
        "cancelled": False,
        "target": {"type": "dm", "user_ids": sorted(list(ids))},
        "created_by": interaction.user.id,
    }
    data["events"].append(ev)
    save_data(data)

    await interaction.followup.send(
        f"✅ Privater Termin gespeichert. ID: **{eid}**. Empfänger: **{len(ids)}**",
        ephemeral=True
    )

@bot.tree.command(name="termine", description="Zeigt nur aktive (zukünftige) Termine")
async def termine_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    now = now_berlin()

    events = []
    for e in data["events"]:
        if e.get("cancelled", False):
            continue
        dt = dt_from_iso(e["datetime"])
        if dt >= now:
            events.append(e)

    events.sort(key=lambda x: dt_from_iso(x["datetime"]))

    if not events:
        await interaction.followup.send("📭 Keine aktiven Termine.", ephemeral=True)
        return

    lines = []
    for e in events[:25]:
        dt = dt_from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        rec = e.get("recurrence", "none")
        target = e["target"]["type"]
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {rec} · {target}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termine_all", description="Zeigt alle Termine (auch alte/abgesagte)")
async def termine_all_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    events = data["events"][:]
    events.sort(key=lambda x: dt_from_iso(x["datetime"]))

    if not events:
        await interaction.followup.send("📭 Keine Termine gespeichert.", ephemeral=True)
        return

    lines = []
    for e in events[:25]:
        dt = dt_from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        rec = e.get("recurrence", "none")
        target = e["target"]["type"]
        status = "abgesagt/erledigt" if e.get("cancelled", False) else "aktiv"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {rec} · {target} · {status}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termin_absagen", description="Sagt einen Termin ab (per ID)")
@app_commands.describe(termin_id="ID aus /termine oder /termine_all")
async def termin_absagen_cmd(interaction: discord.Interaction, termin_id: int):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    found = False
    for e in data["events"]:
        if int(e.get("id", -1)) == int(termin_id) and not e.get("cancelled", False):
            e["cancelled"] = True
            found = True
            break

    if found:
        save_data(data)
        await interaction.followup.send(f"❌ Termin **{termin_id}** abgesagt.", ephemeral=True)
    else:
        await interaction.followup.send("❌ Termin-ID nicht gefunden oder schon abgesagt.", ephemeral=True)

@bot.tree.command(name="termin_edit", description="Bearbeitet einen Termin (per ID)")
@app_commands.describe(
    termin_id="ID aus /termine",
    datum="Optional: DD.MM.YYYY",
    uhrzeit="Optional: HH:MM",
    titel="Optional: neuer Titel",
    erinnerung="Optional: z.B. 120,30,10",
    wiederholung="Optional: none/daily/weekly/monthly"
)
@app_commands.choices(wiederholung=REC_CHOICES)
async def termin_edit_cmd(
    interaction: discord.Interaction,
    termin_id: int,
    datum: Optional[str] = None,
    uhrzeit: Optional[str] = None,
    titel: Optional[str] = None,
    erinnerung: Optional[str] = None,
    wiederholung: Optional[app_commands.Choice[str]] = None,
):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    ev = None
    for e in data["events"]:
        if int(e.get("id", -1)) == int(termin_id) and not e.get("cancelled", False):
            ev = e
            break

    if ev is None:
        await interaction.followup.send("❌ Termin-ID nicht gefunden.", ephemeral=True)
        return

    if titel and titel.strip():
        ev["title"] = titel.strip()

    if erinnerung is not None:
        ev["reminders"] = parse_reminders(erinnerung)
        ev["sent"] = []  # reset

    if wiederholung is not None:
        ev["recurrence"] = wiederholung.value

    if datum is not None or uhrzeit is not None:
        current_dt = dt_from_iso(ev["datetime"])
        d = datum if datum is not None else current_dt.strftime("%d.%m.%Y")
        t = uhrzeit if uhrzeit is not None else current_dt.strftime("%H:%M")
        try:
            new_dt = parse_date_time(d, t)
        except Exception:
            await interaction.followup.send("❌ Neues Datum/Uhrzeit ungültig.", ephemeral=True)
            return
        ev["datetime"] = dt_to_iso(new_dt)
        ev["sent"] = []

    save_data(data)
    await interaction.followup.send(f"✅ Termin **{termin_id}** aktualisiert.", ephemeral=True)

# =========================
# Start
# =========================
async def main():
    async with bot:
        bot.loop.create_task(reminder_loop())
        await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
