import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Set

import discord
from discord import app_commands

# =========================
# ENV / KONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
CLEAN_GLOBAL_COMMANDS = os.environ.get("CLEAN_GLOBAL_COMMANDS", "0").strip() == "1"

TZ = ZoneInfo("Europe/Berlin")
DATA_FILE = "data.json"
AUTO_DELETE_SECONDS = 900  # 15 Minuten
CHECK_INTERVAL_SECONDS = 20

CHOICES_REC = [
    app_commands.Choice(name="none", value="none"),
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="monthly", value="monthly"),
]

TODO_VIS_CHOICES = [
    app_commands.Choice(name="private", value="private"),  # nur für dich
    app_commands.Choice(name="user", value="user"),        # für ausgewählte User
    app_commands.Choice(name="role", value="role"),        # für eine Rolle
]

# =========================
# Helpers (Zeit / Persistenz)
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
    # DD.MM.YYYY HH:MM
    naive = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return naive.replace(tzinfo=TZ)

def parse_reminders(rem_str: str) -> List[int]:
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
            out.append(int(p))
    return sorted(set(x for x in out if x >= 0), reverse=True)

def load_data() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data.setdefault("events", [])
    data.setdefault("next_id", 1)

    data.setdefault("todos", [])
    data.setdefault("next_todo_id", 1)

    return data

def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def new_event_id(data: Dict[str, Any]) -> int:
    nid = int(data.get("next_id", 1))
    data["next_id"] = nid + 1
    return nid

def new_todo_id(data: Dict[str, Any]) -> int:
    nid = int(data.get("next_todo_id", 1))
    data["next_todo_id"] = nid + 1
    return nid

def add_month(dt: datetime) -> datetime:
    y, m = dt.year, dt.month + 1
    if m == 13:
        y += 1
        m = 1

    if m == 12:
        next_month = datetime(y + 1, 1, 1, tzinfo=dt.tzinfo)
    else:
        next_month = datetime(y, m + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day
    return dt.replace(year=y, month=m, day=min(dt.day, last_day))

def next_occurrence(dt: datetime, recurrence: str) -> datetime:
    recurrence = (recurrence or "none").lower()
    if recurrence == "daily":
        return dt + timedelta(days=1)
    if recurrence == "weekly":
        return dt + timedelta(weeks=1)
    if recurrence == "monthly":
        return add_month(dt)
    return dt

def build_reminder_message(title: str, dt: datetime, minutes_before: int) -> str:
    when = dt.strftime("%d.%m.%Y %H:%M")
    return (
        f"🔔 **Erinnerung** ({minutes_before} min vorher)\n"
        f"📌 **{title}**\n"
        f"🕒 {when} (Berlin)"
    )

# =========================
# Discord Client (Slash only)
# =========================
class MyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # für Member-Auswahl in /ptermin und Todo-User
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

bot = MyBot()

async def send_channel_message(channel_id: int, content: str):
    ch = bot.get_channel(channel_id)
    if ch is None:
        ch = await bot.fetch_channel(channel_id)
    await ch.send(content, delete_after=AUTO_DELETE_SECONDS)

async def send_dm(user_id: int, content: str):
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    await user.send(content)

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    perms = interaction.user.guild_permissions
    return perms.manage_guild or perms.administrator

def member_role_ids(member: discord.Member) -> Set[int]:
    return {r.id for r in member.roles}

# =========================
# Sync (wichtig: copy_global_to)
# =========================
async def do_sync():
    guild = discord.Object(id=GUILD_ID)

    if CLEAN_GLOBAL_COMMANDS:
        print("🧹 CLEAN_GLOBAL_COMMANDS: Lösche globale Slash-Commands …", flush=True)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("✅ Globale Slash-Commands gelöscht.", flush=True)

    # 🔥 Fix: globale Commands in die Guild kopieren, dann guild-sync
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    remote = await bot.tree.fetch_commands(guild=guild)
    print(f"✅ Slash-Commands synced to guild {GUILD_ID}", flush=True)
    print(f"📌 Remote Commands (Guild): {[c.name for c in remote]}", flush=True)

@bot.event
async def setup_hook():
    await do_sync()
    bot.loop.create_task(reminder_loop())

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)

# =========================
# Reminder Loop (Termine)
# =========================
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
                reminders: List[int] = [int(x) for x in ev.get("reminders", [])]
                sent = set(int(x) for x in ev.get("sent", []))

                for m in reminders:
                    if m in sent:
                        continue
                    if now >= (dt - timedelta(minutes=m)) and now < dt + timedelta(hours=24):
                        msg = build_reminder_message(ev["title"], dt, m)

                        if ev["target"]["type"] == "channel":
                            await send_channel_message(ev["target"]["channel_id"], f"<@&{ROLLE_ID}> {msg}")
                        else:
                            for uid in ev["target"]["user_ids"]:
                                await send_dm(uid, msg)

                        sent.add(m)
                        ev["sent"] = sorted(list(sent), reverse=True)
                        changed = True

                if now >= dt:
                    rec = (ev.get("recurrence") or "none").lower()
                    if rec != "none":
                        ev["datetime"] = dt_to_iso(next_occurrence(dt, rec))
                        ev["sent"] = []
                    else:
                        ev["cancelled"] = True
                    changed = True

            if changed:
                save_data(data)

        except Exception as e:
            print(f"❌ Fehler im Reminder-Loop: {type(e).__name__}: {e}", flush=True)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# =========================
# /ping
# =========================
@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping_cmd(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f"🏓 Pong! Latenz: `{latency} ms`", ephemeral=True)

# =========================
# Termine (Slash)
# =========================
@bot.tree.command(name="termin", description="Öffentlicher Termin (Channel) mit Rollen-Ping")
@app_commands.describe(
    datum="DD.MM.YYYY (z.B. 08.02.2026)",
    uhrzeit="HH:MM (z.B. 12:00)",
    titel="Titel des Termins",
    erinnerung="Mehrere Erinnerungen: z.B. 60,10,5 oder 1h,10m",
    wiederholung="none/daily/weekly/monthly",
)
@app_commands.choices(wiederholung=CHOICES_REC)
async def termin_cmd(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerung: str = "30",
    wiederholung: str = "none",
):
    await interaction.response.defer(ephemeral=True)

    try:
        dt = parse_date_time(datum, uhrzeit)
    except Exception:
        await interaction.followup.send("❌ Datum/Uhrzeit ungültig. Beispiel: 08.02.2026 und 12:00", ephemeral=True)
        return

    reminders = parse_reminders(erinnerung)
    data = load_data()
    eid = new_event_id(data)

    data["events"].append({
        "id": eid,
        "title": titel,
        "datetime": dt_to_iso(dt),
        "reminders": reminders,
        "sent": [],
        "recurrence": wiederholung,
        "cancelled": False,
        "target": {"type": "channel", "channel_id": ERINNERUNGS_CHANNEL_ID},
        "created_by": interaction.user.id,
    })
    save_data(data)

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
    erinnerung="Mehrere Erinnerungen: z.B. 60,10,5 oder 1h,10m",
    wiederholung="none/daily/weekly/monthly",
    person1="Optional",
    person2="Optional",
    person3="Optional",
    person4="Optional",
    person5="Optional",
)
@app_commands.choices(wiederholung=CHOICES_REC)
async def ptermin_cmd(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerung: str = "30",
    wiederholung: str = "none",
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
    ids = {interaction.user.id}
    for p in (person1, person2, person3, person4, person5):
        if p:
            ids.add(p.id)

    data = load_data()
    eid = new_event_id(data)

    data["events"].append({
        "id": eid,
        "title": titel,
        "datetime": dt_to_iso(dt),
        "reminders": reminders,
        "sent": [],
        "recurrence": wiederholung,
        "cancelled": False,
        "target": {"type": "dm", "user_ids": sorted(list(ids))},
        "created_by": interaction.user.id,
    })
    save_data(data)

    await interaction.followup.send(
        f"✅ Privater Termin gespeichert. ID: **{eid}**. Empfänger: **{len(ids)}**",
        ephemeral=True,
    )

@bot.tree.command(name="termine", description="Zeigt nur aktive (zukünftige) Termine")
async def termine_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    now = now_berlin()
    events = [e for e in data["events"] if not e.get("cancelled", False) and dt_from_iso(e["datetime"]) >= now]
    events.sort(key=lambda e: dt_from_iso(e["datetime"]))

    if not events:
        await interaction.followup.send("📭 Keine aktiven Termine.", ephemeral=True)
        return

    lines = []
    for e in events[:25]:
        dt = dt_from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termine_all", description="Zeigt alle Termine (auch alte/abgesagte)")
async def termine_all_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    events = sorted(data["events"], key=lambda e: dt_from_iso(e["datetime"]))

    if not events:
        await interaction.followup.send("📭 Keine Termine gespeichert.", ephemeral=True)
        return

    lines = []
    for e in events[:25]:
        dt = dt_from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        status = "abgesagt/erledigt" if e.get("cancelled", False) else "aktiv"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']} · {status}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termin_absagen", description="Sagt einen Termin ab (per ID)")
@app_commands.describe(termin_id="ID aus /termine oder /termine_all")
async def termin_absagen_cmd(interaction: discord.Interaction, termin_id: int):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    for e in data["events"]:
        if int(e.get("id", -1)) == int(termin_id) and not e.get("cancelled", False):
            e["cancelled"] = True
            save_data(data)
            await interaction.followup.send(f"❌ Termin **{termin_id}** abgesagt.", ephemeral=True)
            return
    await interaction.followup.send("❌ Termin-ID nicht gefunden oder schon abgesagt.", ephemeral=True)

@bot.tree.command(name="termin_edit", description="Bearbeitet einen Termin (per ID)")
@app_commands.describe(
    termin_id="ID aus /termine",
    datum="Optional: DD.MM.YYYY",
    uhrzeit="Optional: HH:MM",
    titel="Optional: neuer Titel",
    erinnerung="Optional: z.B. 120,30,10",
    wiederholung="Optional: none/daily/weekly/monthly",
)
@app_commands.choices(wiederholung=CHOICES_REC)
async def termin_edit_cmd(
    interaction: discord.Interaction,
    termin_id: int,
    datum: Optional[str] = None,
    uhrzeit: Optional[str] = None,
    titel: Optional[str] = None,
    erinnerung: Optional[str] = None,
    wiederholung: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    ev = next((e for e in data["events"] if int(e.get("id", -1)) == int(termin_id) and not e.get("cancelled", False)), None)

    if ev is None:
        await interaction.followup.send("❌ Termin-ID nicht gefunden.", ephemeral=True)
        return

    if titel and titel.strip():
        ev["title"] = titel.strip()
    if erinnerung is not None:
        ev["reminders"] = parse_reminders(erinnerung)
        ev["sent"] = []
    if wiederholung is not None:
        ev["recurrence"] = wiederholung
    if datum is not None or uhrzeit is not None:
        cur = dt_from_iso(ev["datetime"])
        d = datum if datum is not None else cur.strftime("%d.%m.%Y")
        t = uhrzeit if uhrzeit is not None else cur.strftime("%H:%M")
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
# TODOS (Slash)
# =========================
def todo_relevant_for_member(todo: Dict[str, Any], member: discord.Member) -> bool:
    if todo.get("deleted", False):
        return False
    vis = todo.get("visibility", "private")
    if vis == "private":
        return int(todo.get("created_by", -1)) == member.id
    if vis == "user":
        return member.id in set(int(x) for x in todo.get("user_ids", []))
    if vis == "role":
        rid = todo.get("role_id")
        if not rid:
            return False
        return int(rid) in member_role_ids(member)
    return False

def todo_can_edit(todo: Dict[str, Any], interaction: discord.Interaction) -> bool:
    # Ersteller oder Admin dürfen immer
    if int(todo.get("created_by", -1)) == interaction.user.id:
        return True
    return is_admin(interaction)

def todo_can_done(todo: Dict[str, Any], interaction: discord.Interaction) -> bool:
    # Ersteller, Admin oder relevante Assignees dürfen abhaken
    if todo_can_edit(todo, interaction):
        return True
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    return todo_relevant_for_member(todo, interaction.user)

def format_todo_line(todo: Dict[str, Any]) -> str:
    status = "✅" if todo.get("done", False) else "🟦"
    tid = todo.get("id")
    title = todo.get("title", "")
    vis = todo.get("visibility", "private")
    if vis == "private":
        who = "privat"
    elif vis == "user":
        uids = todo.get("user_ids", [])
        who = "user: " + ", ".join(f"<@{int(x)}>" for x in uids[:5]) + (" …" if len(uids) > 5 else "")
    elif vis == "role":
        rid = todo.get("role_id")
        who = f"rolle: <@&{int(rid)}>" if rid else "rolle: ?"
    else:
        who = vis

    return f"{status} **{tid}** · **{title}** · _{who}_"

@bot.tree.command(name="todo", description="Erstellt ein Todo (private / user / role)")
@app_commands.describe(
    titel="Kurzer Titel",
    beschreibung="Optional",
    sichtbarkeit="private/user/role",
    rolle="Nur bei sichtbarkeit=role",
    user1="Nur bei sichtbarkeit=user",
    user2="Optional",
    user3="Optional",
    user4="Optional",
    user5="Optional",
)
@app_commands.choices(sichtbarkeit=TODO_VIS_CHOICES)
async def todo_create_cmd(
    interaction: discord.Interaction,
    titel: str,
    beschreibung: Optional[str] = None,
    sichtbarkeit: str = "private",
    rolle: Optional[discord.Role] = None,
    user1: Optional[discord.Member] = None,
    user2: Optional[discord.Member] = None,
    user3: Optional[discord.Member] = None,
    user4: Optional[discord.Member] = None,
    user5: Optional[discord.Member] = None,
):
    await interaction.response.defer(ephemeral=True)

    sichtbarkeit = (sichtbarkeit or "private").lower().strip()
    if sichtbarkeit not in ("private", "user", "role"):
        sichtbarkeit = "private"

    user_ids: List[int] = []
    role_id: Optional[int] = None

    if sichtbarkeit == "role":
        if rolle is None:
            await interaction.followup.send("❌ Bei sichtbarkeit=`role` musst du eine Rolle auswählen.", ephemeral=True)
            return
        role_id = rolle.id

    if sichtbarkeit == "user":
        ids = set()
        for u in (user1, user2, user3, user4, user5):
            if u:
                ids.add(u.id)
        # sinnvoll: Ersteller auch automatisch rein
        ids.add(interaction.user.id)
        user_ids = sorted(list(ids))
        if len(user_ids) <= 1:
            await interaction.followup.send("❌ Bei sichtbarkeit=`user` musst du mindestens 1 weiteren User auswählen.", ephemeral=True)
            return

    data = load_data()
    tid = new_todo_id(data)

    todo = {
        "id": tid,
        "title": titel.strip(),
        "description": (beschreibung or "").strip(),
        "created_by": interaction.user.id,
        "created_at": dt_to_iso(now_berlin()),
        "visibility": sichtbarkeit,
        "user_ids": user_ids,
        "role_id": role_id,
        "done": False,
        "done_at": None,
        "done_by": None,
        "deleted": False,
    }
    data["todos"].append(todo)
    save_data(data)

    await interaction.followup.send(f"✅ Todo erstellt: **{tid}** · **{todo['title']}**", ephemeral=True)

@bot.tree.command(name="todos", description="Zeigt Todos, die für dich relevant sind (private + user + rollen)")
async def todos_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Dieser Befehl funktioniert nur auf dem Server (nicht in DMs).", ephemeral=True)
        return

    data = load_data()
    relevant = [t for t in data["todos"] if todo_relevant_for_member(t, interaction.user)]
    relevant.sort(key=lambda x: (x.get("done", False), int(x.get("id", 0))))

    if not relevant:
        await interaction.followup.send("📭 Keine relevanten Todos gefunden.", ephemeral=True)
        return

    lines = [format_todo_line(t) for t in relevant[:25]]
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="todos_user", description="Zeigt Todos für einen bestimmten User (wenn du Zugriff hast)")
@app_commands.describe(user="User auswählen")
async def todos_user_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Nur auf dem Server nutzbar.", ephemeral=True)
        return

    data = load_data()
    res = []
    for t in data["todos"]:
        if t.get("deleted", False):
            continue
        if t.get("visibility") == "user" and user.id in set(int(x) for x in t.get("user_ids", [])):
            # Zugriff: Admin oder Ersteller oder du bist selbst in der Liste
            if is_admin(interaction) or int(t.get("created_by", -1)) == interaction.user.id or user.id == interaction.user.id:
                res.append(t)

    res.sort(key=lambda x: (x.get("done", False), int(x.get("id", 0))))

    if not res:
        await interaction.followup.send("📭 Keine passenden User-Todos gefunden (oder kein Zugriff).", ephemeral=True)
        return

    await interaction.followup.send("\n".join(format_todo_line(t) for t in res[:25]), ephemeral=True)

@bot.tree.command(name="todos_role", description="Zeigt Todos für eine Rolle (wenn du Zugriff hast)")
@app_commands.describe(rolle="Rolle auswählen")
async def todos_role_cmd(interaction: discord.Interaction, rolle: discord.Role):
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Nur auf dem Server nutzbar.", ephemeral=True)
        return

    data = load_data()
    res = []
    for t in data["todos"]:
        if t.get("deleted", False):
            continue
        if t.get("visibility") == "role" and int(t.get("role_id") or 0) == rolle.id:
            # Zugriff: Admin oder Ersteller oder du hast die Rolle
            if is_admin(interaction) or int(t.get("created_by", -1)) == interaction.user.id or rolle.id in member_role_ids(interaction.user):
                res.append(t)

    res.sort(key=lambda x: (x.get("done", False), int(x.get("id", 0))))

    if not res:
        await interaction.followup.send("📭 Keine passenden Rollen-Todos gefunden (oder kein Zugriff).", ephemeral=True)
        return

    await interaction.followup.send("\n".join(format_todo_line(t) for t in res[:25]), ephemeral=True)

@bot.tree.command(name="todo_done", description="Hakt ein Todo ab")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_done_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    data = load_data()

    todo = next((t for t in data["todos"] if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)), None)
    if not todo:
        await interaction.followup.send("❌ Todo nicht gefunden.", ephemeral=True)
        return

    if not todo_can_done(todo, interaction):
        await interaction.followup.send("❌ Du hast keine Berechtigung, dieses Todo abzuhaken.", ephemeral=True)
        return

    todo["done"] = True
    todo["done_at"] = dt_to_iso(now_berlin())
    todo["done_by"] = interaction.user.id
    save_data(data)

    await interaction.followup.send(f"✅ Todo **{todo_id}** abgehakt.", ephemeral=True)

@bot.tree.command(name="todo_undo", description="Setzt ein Todo wieder auf offen")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_undo_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    data = load_data()

    todo = next((t for t in data["todos"] if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)), None)
    if not todo:
        await interaction.followup.send("❌ Todo nicht gefunden.", ephemeral=True)
        return

    if not todo_can_done(todo, interaction):
        await interaction.followup.send("❌ Du hast keine Berechtigung, dieses Todo zu ändern.", ephemeral=True)
        return

    todo["done"] = False
    todo["done_at"] = None
    todo["done_by"] = None
    save_data(data)

    await interaction.followup.send(f"🟦 Todo **{todo_id}** ist wieder offen.", ephemeral=True)

@bot.tree.command(name="todo_delete", description="Löscht ein Todo (Ersteller oder Admin)")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_delete_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    data = load_data()

    todo = next((t for t in data["todos"] if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)), None)
    if not todo:
        await interaction.followup.send("❌ Todo nicht gefunden.", ephemeral=True)
        return

    if not todo_can_edit(todo, interaction):
        await interaction.followup.send("❌ Du kannst dieses Todo nicht löschen (nur Ersteller/Admin).", ephemeral=True)
        return

    todo["deleted"] = True
    save_data(data)
    await interaction.followup.send(f"🗑️ Todo **{todo_id}** gelöscht.", ephemeral=True)

# =========================
# Start
# =========================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
