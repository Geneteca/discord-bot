import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Set

import discord
from discord.ext import commands
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
AUTO_DELETE_SECONDS = 900          # Channel-Nachrichten nach 15min löschen
CHECK_INTERVAL_SECONDS = 20        # Reminder-Loop Tick

CHOICES_REC = [
    app_commands.Choice(name="none", value="none"),
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="monthly", value="monthly"),
]

# =========================
# Helpers
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
    # DD.MM.YYYY + HH:MM
    naive = datetime.strptime(f"{date_str} {time_str}", "%d.%m.%Y %H:%M")
    return naive.replace(tzinfo=TZ)

def parse_reminders(rem_str: str) -> List[int]:
    """
    Beispiele:
      "60,10,5"
      "1h,10m"
      "30"
      "1d"
    -> Minuten (unique, absteigend)
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
            out.append(int(p))
    return sorted(set(x for x in out if x >= 0), reverse=True)

def load_data() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

    data.setdefault("events", [])
    data.setdefault("next_event_id", 1)

    data.setdefault("todos", [])
    data.setdefault("next_todo_id", 1)

    return data

def save_data(data: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def new_event_id(data: Dict[str, Any]) -> int:
    nid = int(data.get("next_event_id", 1))
    data["next_event_id"] = nid + 1
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
    # letzter Tag im Zielmonat
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

def fmt_due(due_iso: Optional[str]) -> str:
    if not due_iso:
        return ""
    try:
        dt = dt_from_iso(due_iso)
        return f" · fällig: {dt.strftime('%d.%m.%Y %H:%M')}"
    except Exception:
        return ""

# =========================
# Discord Bot
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # nötig für Member/Role-Parameter in Slash-Commands

bot = commands.Bot(command_prefix="!", intents=intents)  # Prefix wird nicht genutzt, nur tree

async def send_channel_message(channel_id: int, content: str):
    ch = bot.get_channel(channel_id)
    if ch is None:
        ch = await bot.fetch_channel(channel_id)
    await ch.send(content, delete_after=AUTO_DELETE_SECONDS)

async def send_dm(user_id: int, content: str):
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    await user.send(content)

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)

# =========================
# Sync (Fix: copy_global_to)
# =========================
async def do_sync():
    guild = discord.Object(id=GUILD_ID)

    if CLEAN_GLOBAL_COMMANDS:
        print("🧹 CLEAN_GLOBAL_COMMANDS: Lösche globale Slash-Commands …", flush=True)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("✅ Globale Slash-Commands gelöscht.", flush=True)

    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    remote = await bot.tree.fetch_commands(guild=guild)
    print(f"✅ Slash-Commands synced to guild {GUILD_ID}", flush=True)
    print(f"📌 Remote Commands (Guild): {[c.name for c in remote]}", flush=True)

@bot.event
async def setup_hook():
    await do_sync()
    bot.loop.create_task(reminder_loop())

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

                # Erinnerungen senden
                for m in reminders:
                    if m in sent:
                        continue
                    if now >= (dt - timedelta(minutes=m)) and now < dt + timedelta(hours=24):
                        msg = build_reminder_message(ev["title"], dt, m)

                        if ev["target"]["type"] == "channel":
                            # Rollen-Ping im Channel
                            await send_channel_message(ev["target"]["channel_id"], f"<@&{ROLLE_ID}> {msg}")
                        else:
                            # DM ohne Ping
                            for uid in ev["target"]["user_ids"]:
                                await send_dm(uid, msg)

                        sent.add(m)
                        ev["sent"] = sorted(list(sent), reverse=True)
                        changed = True

                # Terminzeit vorbei -> wiederkehrend oder abschließen
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
# Todo Rechte / Relevanz
# =========================
def user_role_ids(member: discord.Member) -> Set[int]:
    return {r.id for r in getattr(member, "roles", [])}

def todo_is_relevant(todo: Dict[str, Any], member: discord.Member) -> bool:
    if todo.get("deleted", False):
        return False
    scope = todo.get("scope", "public")  # public/private/user/role
    if scope == "public":
        return True
    if scope == "private":
        return int(todo.get("created_by", 0)) == member.id
    if scope == "user":
        return int(todo.get("assigned_user_id", 0)) == member.id or int(todo.get("created_by", 0)) == member.id
    if scope == "role":
        rid = int(todo.get("assigned_role_id", 0))
        return rid in user_role_ids(member) or int(todo.get("created_by", 0)) == member.id
    return False

def can_modify_todo(todo: Dict[str, Any], member: discord.Member) -> bool:
    if int(todo.get("created_by", 0)) == member.id:
        return True
    if todo.get("scope") == "user" and int(todo.get("assigned_user_id", 0)) == member.id:
        return True
    if member.guild_permissions.manage_guild:
        return True
    return False

# =========================
# SLASH: /help /ping
# =========================
@bot.tree.command(name="help", description="Übersicht aller Commands")
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**ℹ️ Allgemein**\n"
        "/ping – Bot-Status & Latenz\n"
        "/help – Diese Übersicht\n\n"
        "**📅 Termine**\n"
        "/termin – Öffentlichen Termin im Channel erstellen (mit Rollen-Ping)\n"
        "/ptermin – Privaten Termin erstellen (DM an dich + ausgewählte Personen)\n"
        "/termine – Zeigt nur aktive (zukünftige) Termine\n"
        "/termine_all – Zeigt alle Termine (inkl. alte/abgesagte)\n"
        "/termin_edit – Termin bearbeiten (ID)\n"
        "/termin_absagen – Termin absagen (ID)\n\n"
        "**📝 Todos**\n"
        "/todo – Todo erstellen (öffentlich/privat/user/rolle)\n"
        "/todos – Zeigt nur offene, relevante Todos\n"
        "/oldtodos – Zeigt erledigte, relevante Todos\n"
        "/todo_done – Todo abhaken (ID)\n"
        "/todo_undo – Todo wieder öffnen (ID)\n"
        "/todo_edit – Todo bearbeiten (ID)\n"
        "/todo_delete – Todo löschen (ID)\n"
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping_cmd(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(f" Bin Online du Hond Latenz: `{latency} ms`", ephemeral=True)

# =========================
# SLASH: Termine
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
        "title": titel.strip(),
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
        "title": titel.strip(),
        "datetime": dt_to_iso(dt),
        "reminders": reminders,
        "sent": [],
        "recurrence": wiederholung,
        "cancelled": False,
        "target": {"type": "dm", "user_ids": sorted(list(ids))},
        "created_by": interaction.user.id,
    })
    save_data(data)

    await interaction.followup.send(f"✅ Privater Termin gespeichert. ID: **{eid}**. Empfänger: **{len(ids)}**", ephemeral=True)

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
# SLASH: Todos
# =========================
@bot.tree.command(name="todo", description="Erstellt ein Todo (public/private/user/role)")
@app_commands.describe(
    titel="Kurzbeschreibung",
    beschreibung="Optional",
    privat="Wenn true: nur du siehst es",
    user="Optional: Todo einer Person zuweisen",
    rolle="Optional: Todo einer Rolle zuweisen",
    faellig_datum="Optional: DD.MM.YYYY",
    faellig_uhrzeit="Optional: HH:MM (wenn Datum gesetzt)",
)
async def todo_add_cmd(
    interaction: discord.Interaction,
    titel: str,
    beschreibung: Optional[str] = None,
    privat: bool = False,
    user: Optional[discord.Member] = None,
    rolle: Optional[discord.Role] = None,
    faellig_datum: Optional[str] = None,
    faellig_uhrzeit: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)

    # Regeln: privat überschreibt alles
    scope = "public"
    assigned_user_id = None
    assigned_role_id = None

    if privat:
        scope = "private"
    else:
        if user is not None and rolle is not None:
            await interaction.followup.send("❌ Bitte entweder **user** oder **rolle** setzen (nicht beides).", ephemeral=True)
            return
        if user is not None:
            scope = "user"
            assigned_user_id = user.id
        elif rolle is not None:
            scope = "role"
            assigned_role_id = rolle.id
        else:
            scope = "public"

    due_iso = None
    if faellig_datum:
        t = faellig_uhrzeit if faellig_uhrzeit else "23:59"
        try:
            due_dt = parse_date_time(faellig_datum, t)
            due_iso = dt_to_iso(due_dt)
        except Exception:
            await interaction.followup.send("❌ Fälligkeit ungültig. Beispiel: 10.03.2026 und 18:30", ephemeral=True)
            return

    data = load_data()
    tid = new_todo_id(data)

    todo = {
        "id": tid,
        "title": titel.strip(),
        "description": (beschreibung or "").strip(),
        "scope": scope,
        "assigned_user_id": assigned_user_id,
        "assigned_role_id": assigned_role_id,
        "created_by": interaction.user.id,
        "created_at": dt_to_iso(now_berlin()),
        "due": due_iso,
        "done": False,
        "done_at": None,
        "deleted": False,
    }
    data["todos"].append(todo)
    save_data(data)

    target_txt = "🌍 öffentlich"
    if scope == "private":
        target_txt = "🔒 privat"
    elif scope == "user":
        target_txt = f"👤 für {user.display_name}"
    elif scope == "role":
        target_txt = f"👥 für @{rolle.name}"

    await interaction.followup.send(
        f"✅ Todo erstellt: **{tid}** · **{todo['title']}** ({target_txt}){fmt_due(due_iso)}",
        ephemeral=True,
    )

@bot.tree.command(name="todos", description="Zeigt offene, relevante Todos")
async def todos_list_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
        return
    member: discord.Member = interaction.user

    data = load_data()
    todos = [t for t in data["todos"] if not t.get("deleted", False) and not t.get("done", False) and todo_is_relevant(t, member)]

    def sort_key(t: Dict[str, Any]):
        due = t.get("due")
        due_dt = dt_from_iso(due) if due else datetime.max.replace(tzinfo=TZ)
        created = dt_from_iso(t.get("created_at")) if t.get("created_at") else now_berlin()
        return (due_dt, created)

    todos.sort(key=sort_key)

    if not todos:
        await interaction.followup.send("📭 Keine offenen Todos.", ephemeral=True)
        return

    lines = []
    for t in todos[:40]:
        due_txt = fmt_due(t.get("due"))
        desc = t.get("description", "")
        if desc:
            desc = f" — {desc[:60]}" + ("…" if len(desc) > 60 else "")

        scope = t.get("scope", "public")
        tag = "öffentlich"
        if scope == "private":
            tag = "privat"
        elif scope == "user":
            tag = "user"
        elif scope == "role":
            tag = "rolle"

        lines.append(f"⬜ **{t['id']}** · **{t['title']}** ({tag}){due_txt}{desc}")

    if len(todos) > 40:
        lines.append(f"… und {len(todos) - 40} weitere.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="oldtodos", description="Zeigt erledigte, relevante Todos")
async def oldtodos_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
        return
    member: discord.Member = interaction.user

    data = load_data()
    todos = [t for t in data["todos"] if not t.get("deleted", False) and t.get("done", False) and todo_is_relevant(t, member)]

    def sort_key(t: Dict[str, Any]):
        done_at = t.get("done_at")
        dt = dt_from_iso(done_at) if done_at else datetime.min.replace(tzinfo=TZ)
        return dt

    todos.sort(key=sort_key, reverse=True)

    if not todos:
        await interaction.followup.send("📭 Keine erledigten Todos.", ephemeral=True)
        return

    lines = []
    for t in todos[:40]:
        done_txt = ""
        if t.get("done_at"):
            done_dt = dt_from_iso(t["done_at"])
            done_txt = f" · erledigt: {done_dt.strftime('%d.%m.%Y %H:%M')}"
        lines.append(f"✅ **{t['id']}** · **{t['title']}**{done_txt}")

    if len(todos) > 40:
        lines.append(f"… und {len(todos) - 40} weitere.")

    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="todo_done", description="Hakt ein Todo ab (per ID)")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_done_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
        return
    member: discord.Member = interaction.user

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)), None)
    if not todo:
        await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
        return

    if not can_modify_todo(todo, member):
        await interaction.followup.send("❌ Du darfst dieses Todo nicht abhaken.", ephemeral=True)
        return

    if todo.get("done", False):
        await interaction.followup.send("ℹ️ Dieses Todo ist bereits erledigt.", ephemeral=True)
        return

    todo["done"] = True
    todo["done_at"] = dt_to_iso(now_berlin())
    save_data(data)
    await interaction.followup.send(f"✅ Todo **{todo_id}** abgehakt.", ephemeral=True)

@bot.tree.command(name="todo_undo", description="Setzt ein Todo wieder auf offen (per ID)")
@app_commands.describe(todo_id="ID aus /oldtodos")
async def todo_undo_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
        return
    member: discord.Member = interaction.user

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)), None)
    if not todo:
        await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
        return

    if not can_modify_todo(todo, member):
        await interaction.followup.send("❌ Du darfst dieses Todo nicht ändern.", ephemeral=True)
        return

    todo["done"] = False
    todo["done_at"] = None
    save_data(data)
    await interaction.followup.send(f"↩️ Todo **{todo_id}** wieder offen.", ephemeral=True)

@bot.tree.command(name="todo_delete", description="Löscht ein Todo (per ID)")
@app_commands.describe(todo_id="ID aus /todos oder /oldtodos")
async def todo_delete_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
        return
    member: discord.Member = interaction.user

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)), None)
    if not todo:
        await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
        return

    if not can_modify_todo(todo, member):
        await interaction.followup.send("❌ Du darfst dieses Todo nicht löschen.", ephemeral=True)
        return

    todo["deleted"] = True
    save_data(data)
    await interaction.followup.send(f"🗑️ Todo **{todo_id}** gelöscht.", ephemeral=True)

@bot.tree.command(name="todo_edit", description="Bearbeitet ein bestehendes Todo")
@app_commands.describe(
    todo_id="ID aus /todos oder /oldtodos",
    titel="Optional: neuer Titel",
    beschreibung="Optional: neue Beschreibung",
    privat="Optional: true = privat, false = nicht privat",
    user="Optional: Todo einer Person zuweisen",
    rolle="Optional: Todo einer Rolle zuweisen",
    faellig_datum="Optional: DD.MM.YYYY (leer = entfernen)",
    faellig_uhrzeit="Optional: HH:MM"
)
async def todo_edit_cmd(
    interaction: discord.Interaction,
    todo_id: int,
    titel: Optional[str] = None,
    beschreibung: Optional[str] = None,
    privat: Optional[bool] = None,
    user: Optional[discord.Member] = None,
    rolle: Optional[discord.Role] = None,
    faellig_datum: Optional[str] = None,
    faellig_uhrzeit: Optional[str] = None,
):
    await interaction.response.defer(ephemeral=True)

    if not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
        return
    member: discord.Member = interaction.user

    data = load_data()
    todo = next(
        (t for t in data["todos"]
         if int(t.get("id", -1)) == int(todo_id) and not t.get("deleted", False)),
        None
    )

    if not todo:
        await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
        return

    if not can_modify_todo(todo, member):
        await interaction.followup.send("❌ Du darfst dieses Todo nicht bearbeiten.", ephemeral=True)
        return

    # Text
    if titel is not None and titel.strip():
        todo["title"] = titel.strip()
    if beschreibung is not None:
        todo["description"] = beschreibung.strip()

    # Scope / Zuweisung
    if privat is True:
        todo["scope"] = "private"
        todo["assigned_user_id"] = None
        todo["assigned_role_id"] = None
    elif privat is False:
        if user is None and rolle is None and todo.get("scope") == "private":
            todo["scope"] = "public"

    if user is not None:
        todo["scope"] = "user"
        todo["assigned_user_id"] = user.id
        todo["assigned_role_id"] = None

    if rolle is not None:
        todo["scope"] = "role"
        todo["assigned_role_id"] = rolle.id
        todo["assigned_user_id"] = None

    # Fälligkeit
    if faellig_datum is not None:
        if faellig_datum.strip() == "":
            todo["due"] = None
        else:
            t = faellig_uhrzeit if faellig_uhrzeit else "23:59"
            try:
                due_dt = parse_date_time(faellig_datum, t)
                todo["due"] = dt_to_iso(due_dt)
            except Exception:
                await interaction.followup.send("❌ Fälligkeit ungültig. Beispiel: 15.03.2026 und 18:00", ephemeral=True)
                return

    save_data(data)
    await interaction.followup.send(f"✅ Todo **{todo_id}** wurde aktualisiert.", ephemeral=True)

# =========================
# Start
# =========================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
