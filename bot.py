import os, json, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Set, Tuple

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
DASHBOARD_CHANNEL_ID = int(os.environ.get("DASHBOARD_CHANNEL_ID", "0"))  # optional

TZ = ZoneInfo("Europe/Berlin")
DATA_FILE = "data.json"
AUTO_DELETE_SECONDS = 900  # 15 Minuten
CHECK_INTERVAL_SECONDS = 20
PAGE_SIZE = 6

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
    return f"🔔 **Erinnerung** ({minutes_before} min vorher)\n📌 **{title}**\n🕒 {when} (Berlin)"

def fmt_due(due_iso: Optional[str]) -> str:
    if not due_iso:
        return ""
    try:
        dt = dt_from_iso(due_iso)
        return f" · fällig: {dt.strftime('%d.%m.%Y %H:%M')}"
    except Exception:
        return ""

# =========================
# Bot
# =========================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def send_channel_message(channel_id: int, content: str, delete_after: Optional[int] = AUTO_DELETE_SECONDS):
    ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await ch.send(content, delete_after=delete_after)

async def send_dm(user_id: int, content: str):
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    await user.send(content)

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)

# =========================
# Sync Fix
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
# Reminder Loop
# =========================
async def reminder_loop():
    await bot.wait_until_ready()
    print("⏰ Reminder-Loop aktiv", flush=True)

    while not bot.is_closed():
        try:
            data = load_data()
            changed = False
            n = now_berlin()

            for ev in data["events"]:
                if ev.get("cancelled", False):
                    continue

                dt = dt_from_iso(ev["datetime"])
                reminders = [int(x) for x in ev.get("reminders", [])]
                sent = set(int(x) for x in ev.get("sent", []))

                for m in reminders:
                    if m in sent:
                        continue
                    if n >= (dt - timedelta(minutes=m)) and n < dt + timedelta(hours=24):
                        msg = build_reminder_message(ev["title"], dt, m)
                        if ev["target"]["type"] == "channel":
                            await send_channel_message(ev["target"]["channel_id"], f"<@&{ROLLE_ID}> {msg}")
                        else:
                            for uid in ev["target"]["user_ids"]:
                                await send_dm(uid, msg)

                        sent.add(m)
                        ev["sent"] = sorted(list(sent), reverse=True)
                        changed = True

                if n >= dt:
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
    scope = todo.get("scope", "public")
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
# /ping /help
# =========================
@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)} ms`", ephemeral=True)

@bot.tree.command(name="help", description="Übersicht aller Commands")
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**ℹ️ Allgemein**\n"
        "/ping\n"
        "/help\n"
        "/dashboard\n\n"
        "**📅 Termine**\n"
        "/termin, /ptermin\n"
        "/termine, /termine_all\n"
        "/termin_edit, /termin_absagen\n\n"
        "**📝 Todos**\n"
        "/todo\n"
        "/todos, /oldtodos\n"
        "/todo_done, /todo_undo\n"
        "/todo_edit, /todo_delete\n"
    )
    await interaction.response.send_message(text, ephemeral=True)

# =========================
# Termine Slash Commands
# =========================
@bot.tree.command(name="termin", description="Öffentlicher Termin (Channel) mit Rollen-Ping")
@app_commands.describe(datum="DD.MM.YYYY", uhrzeit="HH:MM", titel="Titel", erinnerung="z.B. 60,10,5", wiederholung="none/daily/weekly/monthly")
@app_commands.choices(wiederholung=CHOICES_REC)
async def termin_cmd(interaction: discord.Interaction, datum: str, uhrzeit: str, titel: str, erinnerung: str="30", wiederholung: str="none"):
    await interaction.response.defer(ephemeral=True)
    try:
        dt = parse_date_time(datum, uhrzeit)
    except Exception:
        return await interaction.followup.send("❌ Ungültig. Beispiel: 08.02.2026 & 12:00", ephemeral=True)

    data = load_data()
    eid = new_event_id(data)
    rems = parse_reminders(erinnerung)

    data["events"].append({
        "id": eid,
        "title": titel.strip(),
        "datetime": dt_to_iso(dt),
        "reminders": rems,
        "sent": [],
        "recurrence": wiederholung,
        "cancelled": False,
        "target": {"type":"channel", "channel_id": ERINNERUNGS_CHANNEL_ID},
        "created_by": interaction.user.id
    })
    save_data(data)

    rem_txt = ", ".join(f"{m}m" for m in rems) if rems else "—"
    await send_channel_message(
        ERINNERUNGS_CHANNEL_ID,
        f"<@&{ROLLE_ID}> 📅 **Neuer Termin**\n📌 **{titel}**\n🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n🔔 **Erinnerung:** {rem_txt} vorher\n🆔 ID: **{eid}**",
        delete_after=AUTO_DELETE_SECONDS
    )
    await interaction.followup.send(f"✅ Termin gespeichert. ID: **{eid}**", ephemeral=True)

@bot.tree.command(name="ptermin", description="Privater Termin per DM (ohne Rollen-Ping in DM)")
@app_commands.describe(datum="DD.MM.YYYY", uhrzeit="HH:MM", titel="Titel", erinnerung="z.B. 60,10,5", wiederholung="none/daily/weekly/monthly",
                      person1="Optional", person2="Optional", person3="Optional", person4="Optional", person5="Optional")
@app_commands.choices(wiederholung=CHOICES_REC)
async def ptermin_cmd(
    interaction: discord.Interaction,
    datum: str, uhrzeit: str, titel: str,
    erinnerung: str="30", wiederholung: str="none",
    person1: Optional[discord.Member]=None, person2: Optional[discord.Member]=None, person3: Optional[discord.Member]=None,
    person4: Optional[discord.Member]=None, person5: Optional[discord.Member]=None
):
    await interaction.response.defer(ephemeral=True)
    try:
        dt = parse_date_time(datum, uhrzeit)
    except Exception:
        return await interaction.followup.send("❌ Ungültig. Beispiel: 08.02.2026 & 12:00", ephemeral=True)

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
        "reminders": parse_reminders(erinnerung),
        "sent": [],
        "recurrence": wiederholung,
        "cancelled": False,
        "target": {"type":"dm", "user_ids": sorted(list(ids))},
        "created_by": interaction.user.id
    })
    save_data(data)
    await interaction.followup.send(f"✅ Privater Termin gespeichert. ID: **{eid}**. Empfänger: **{len(ids)}**", ephemeral=True)

@bot.tree.command(name="termine", description="Zeigt nur aktive (zukünftige) Termine")
async def termine_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    n = now_berlin()
    events = [e for e in data["events"] if not e.get("cancelled", False) and dt_from_iso(e["datetime"]) >= n]
    events.sort(key=lambda e: dt_from_iso(e["datetime"]))
    if not events:
        return await interaction.followup.send("📭 Keine aktiven Termine.", ephemeral=True)

    lines = []
    for e in events[:25]:
        dt = dt_from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termine_all", description="Zeigt alle Termine (inkl. alte/abgesagte)")
async def termine_all_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    events = sorted(data["events"], key=lambda e: dt_from_iso(e["datetime"]))
    if not events:
        return await interaction.followup.send("📭 Keine Termine gespeichert.", ephemeral=True)

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
            return await interaction.followup.send(f"❌ Termin **{termin_id}** abgesagt.", ephemeral=True)
    await interaction.followup.send("❌ Termin-ID nicht gefunden oder schon abgesagt.", ephemeral=True)

@bot.tree.command(name="termin_edit", description="Bearbeitet einen Termin (per ID)")
@app_commands.describe(termin_id="ID", datum="Optional DD.MM.YYYY", uhrzeit="Optional HH:MM", titel="Optional", erinnerung="Optional z.B. 120,30,10", wiederholung="Optional")
@app_commands.choices(wiederholung=CHOICES_REC)
async def termin_edit_cmd(
    interaction: discord.Interaction,
    termin_id: int,
    datum: Optional[str]=None,
    uhrzeit: Optional[str]=None,
    titel: Optional[str]=None,
    erinnerung: Optional[str]=None,
    wiederholung: Optional[str]=None
):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    ev = next((e for e in data["events"] if int(e.get("id",-1)) == int(termin_id) and not e.get("cancelled", False)), None)
    if not ev:
        return await interaction.followup.send("❌ Termin-ID nicht gefunden.", ephemeral=True)

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
            ev["datetime"] = dt_to_iso(parse_date_time(d, t))
            ev["sent"] = []
        except Exception:
            return await interaction.followup.send("❌ Neues Datum/Uhrzeit ungültig.", ephemeral=True)

    save_data(data)
    await interaction.followup.send(f"✅ Termin **{termin_id}** aktualisiert.", ephemeral=True)

# =========================
# Todos Slash Commands
# =========================
@bot.tree.command(name="todo", description="Erstellt ein Todo (public/private/user/role)")
@app_commands.describe(
    titel="Kurzbeschreibung",
    beschreibung="Optional",
    privat="true = privat",
    user="Optional: Person",
    rolle="Optional: Rolle",
    faellig_datum="Optional DD.MM.YYYY",
    faellig_uhrzeit="Optional HH:MM"
)
async def todo_add_cmd(
    interaction: discord.Interaction,
    titel: str,
    beschreibung: Optional[str]=None,
    privat: bool=False,
    user: Optional[discord.Member]=None,
    rolle: Optional[discord.Role]=None,
    faellig_datum: Optional[str]=None,
    faellig_uhrzeit: Optional[str]=None
):
    await interaction.response.defer(ephemeral=True)
    if user and rolle:
        return await interaction.followup.send("❌ Bitte entweder user oder rolle setzen (nicht beides).", ephemeral=True)

    scope = "private" if privat else "public"
    au = None
    ar = None
    if not privat and user:
        scope, au = "user", user.id
    if not privat and rolle:
        scope, ar = "role", rolle.id

    due = None
    if faellig_datum:
        try:
            due = dt_to_iso(parse_date_time(faellig_datum, faellig_uhrzeit or "23:59"))
        except Exception:
            return await interaction.followup.send("❌ Fälligkeit ungültig. Beispiel: 10.03.2026 & 18:30", ephemeral=True)

    data = load_data()
    tid = new_todo_id(data)
    data["todos"].append({
        "id": tid,
        "title": titel.strip(),
        "description": (beschreibung or "").strip(),
        "scope": scope,
        "assigned_user_id": au,
        "assigned_role_id": ar,
        "created_by": interaction.user.id,
        "created_at": dt_to_iso(now_berlin()),
        "due": due,
        "done": False,
        "done_at": None,
        "deleted": False
    })
    save_data(data)
    await interaction.followup.send(f"✅ Todo erstellt: **{tid}** · **{titel.strip()}**{fmt_due(due)}", ephemeral=True)

@bot.tree.command(name="todos", description="Zeigt offene, relevante Todos")
async def todos_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    data = load_data()
    items = [t for t in data["todos"] if not t.get("deleted") and not t.get("done") and todo_is_relevant(t, member)]
    if not items:
        return await interaction.followup.send("📭 Keine offenen Todos.", ephemeral=True)

    def key(t):
        due = t.get("due")
        due_dt = dt_from_iso(due) if due else datetime.max.replace(tzinfo=TZ)
        created = dt_from_iso(t.get("created_at")) if t.get("created_at") else now_berlin()
        return (due_dt, created)

    items.sort(key=key)
    lines = []
    for t in items[:40]:
        desc = (t.get("description") or "")
        if desc:
            desc = " — " + desc[:60] + ("…" if len(desc) > 60 else "")
        lines.append(f"⬜ **{t['id']}** · **{t['title']}**{fmt_due(t.get('due'))}{desc}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="oldtodos", description="Zeigt erledigte, relevante Todos")
async def oldtodos_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    data = load_data()
    items = [t for t in data["todos"] if not t.get("deleted") and t.get("done") and todo_is_relevant(t, member)]
    if not items:
        return await interaction.followup.send("📭 Keine erledigten Todos.", ephemeral=True)

    items.sort(key=lambda t: dt_from_iso(t["done_at"]) if t.get("done_at") else datetime.min.replace(tzinfo=TZ), reverse=True)
    lines = []
    for t in items[:40]:
        done_txt = ""
        if t.get("done_at"):
            dd = dt_from_iso(t["done_at"])
            done_txt = f" · erledigt: {dd.strftime('%d.%m.%Y %H:%M')}"
        lines.append(f"✅ **{t['id']}** · **{t['title']}**{done_txt}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="todo_done", description="Hakt ein Todo ab (per ID)")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_done_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id",-1)) == int(todo_id) and not t.get("deleted")), None)
    if not todo:
        return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not can_modify_todo(todo, member):
        return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)

    todo["done"] = True
    todo["done_at"] = dt_to_iso(now_berlin())
    save_data(data)
    await interaction.followup.send(f"✅ Todo **{todo_id}** abgehakt.", ephemeral=True)

@bot.tree.command(name="todo_undo", description="Setzt ein Todo wieder auf offen (per ID)")
@app_commands.describe(todo_id="ID aus /oldtodos")
async def todo_undo_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id",-1)) == int(todo_id) and not t.get("deleted")), None)
    if not todo:
        return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not can_modify_todo(todo, member):
        return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)

    todo["done"] = False
    todo["done_at"] = None
    save_data(data)
    await interaction.followup.send(f"↩️ Todo **{todo_id}** wieder offen.", ephemeral=True)

@bot.tree.command(name="todo_delete", description="Löscht ein Todo (per ID)")
@app_commands.describe(todo_id="ID")
async def todo_delete_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id",-1)) == int(todo_id) and not t.get("deleted")), None)
    if not todo:
        return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not can_modify_todo(todo, member):
        return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)

    todo["deleted"] = True
    save_data(data)
    await interaction.followup.send(f"🗑️ Todo **{todo_id}** gelöscht.", ephemeral=True)

@bot.tree.command(name="todo_edit", description="Bearbeitet ein Todo (per ID)")
@app_commands.describe(todo_id="ID", titel="Optional", beschreibung="Optional", privat="Optional",
                      user="Optional", rolle="Optional", faellig_datum="Optional (leer=entfernen)", faellig_uhrzeit="Optional")
async def todo_edit_cmd(
    interaction: discord.Interaction,
    todo_id: int,
    titel: Optional[str]=None,
    beschreibung: Optional[str]=None,
    privat: Optional[bool]=None,
    user: Optional[discord.Member]=None,
    rolle: Optional[discord.Role]=None,
    faellig_datum: Optional[str]=None,
    faellig_uhrzeit: Optional[str]=None
):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user
    if user and rolle:
        return await interaction.followup.send("❌ Bitte entweder user oder rolle (nicht beides).", ephemeral=True)

    data = load_data()
    todo = next((t for t in data["todos"] if int(t.get("id",-1)) == int(todo_id) and not t.get("deleted")), None)
    if not todo:
        return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not can_modify_todo(todo, member):
        return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)

    if titel and titel.strip():
        todo["title"] = titel.strip()
    if beschreibung is not None:
        todo["description"] = beschreibung.strip()

    if privat is True:
        todo["scope"] = "private"
        todo["assigned_user_id"] = None
        todo["assigned_role_id"] = None
    elif privat is False and user is None and rolle is None and todo.get("scope") == "private":
        todo["scope"] = "public"

    if user is not None:
        todo["scope"] = "user"
        todo["assigned_user_id"] = user.id
        todo["assigned_role_id"] = None
    if rolle is not None:
        todo["scope"] = "role"
        todo["assigned_role_id"] = rolle.id
        todo["assigned_user_id"] = None

    if faellig_datum is not None:
        if faellig_datum.strip() == "":
            todo["due"] = None
        else:
            try:
                todo["due"] = dt_to_iso(parse_date_time(faellig_datum, faellig_uhrzeit or "23:59"))
            except Exception:
                return await interaction.followup.send("❌ Fälligkeit ungültig.", ephemeral=True)

    save_data(data)
    await interaction.followup.send(f"✅ Todo **{todo_id}** aktualisiert.", ephemeral=True)

# ============================================================
# DASHBOARD
# ============================================================
def dash_filter(member: discord.Member, tab: str) -> List[Dict[str, Any]]:
    data = load_data()
    n = now_berlin()

    if tab == "todos_open":
        items = [t for t in data["todos"] if not t.get("deleted") and not t.get("done") and todo_is_relevant(t, member)]
        def key(t):
            due = t.get("due")
            due_dt = dt_from_iso(due) if due else datetime.max.replace(tzinfo=TZ)
            created = dt_from_iso(t.get("created_at")) if t.get("created_at") else n
            return (due_dt, created)
        items.sort(key=key)
        return items

    if tab == "todos_done":
        items = [t for t in data["todos"] if not t.get("deleted") and t.get("done") and todo_is_relevant(t, member)]
        items.sort(key=lambda t: dt_from_iso(t["done_at"]) if t.get("done_at") else datetime.min.replace(tzinfo=TZ), reverse=True)
        return items

    if tab == "events_active":
        items = [e for e in data["events"] if not e.get("cancelled") and dt_from_iso(e["datetime"]) >= n]
        items.sort(key=lambda e: dt_from_iso(e["datetime"]))
        return items

    items = list(data["events"])
    items.sort(key=lambda e: dt_from_iso(e["datetime"]))
    return items

def dash_page(items: List[Dict[str, Any]], page: int) -> Tuple[List[Dict[str, Any]], int, int]:
    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    return items[start:start+PAGE_SIZE], page, pages

def dash_embed(member: discord.Member, tab: str, page: int, selected: Optional[int]) -> discord.Embed:
    items = dash_filter(member, tab)
    slice_, page, pages = dash_page(items, page)
    title_map = {
        "todos_open": "📝 Todos – offen",
        "todos_done": "✅ Todos – erledigt",
        "events_active": "📅 Termine – aktiv",
        "events_all": "📦 Termine – alle",
    }
    emb = discord.Embed(title=f"🧠 Dashboard · {title_map.get(tab, tab)}", color=0x5865F2)
    emb.set_footer(text=f"Seite {page+1}/{pages} · Auswahl: {selected if selected else '—'}")

    if not slice_:
        emb.description = "📭 Keine Einträge."
        return emb

    if tab.startswith("todos"):
        for t in slice_:
            status = "⬜" if not t.get("done") else "✅"
            scope = {"public":"öffentlich","private":"privat","user":"user","role":"rolle"}.get(t.get("scope","public"), t.get("scope","public"))
            desc = (t.get("description") or "—")
            emb.add_field(
                name=f"{status} ID {t['id']} · {t.get('title','—')} ({scope}){fmt_due(t.get('due'))}",
                value=desc[:180] + ("…" if len(desc) > 180 else ""),
                inline=False
            )
    else:
        for e in slice_:
            dt = dt_from_iso(e["datetime"])
            status = "❌" if e.get("cancelled") else "📅"
            rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
            rec = e.get("recurrence", "none")
            tgt = e.get("target", {}).get("type", "channel")
            emb.add_field(
                name=f"{status} ID {e['id']} · {e.get('title','—')}",
                value=f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} · 🔔 {rems} · 🔁 {rec} · 🎯 {tgt}",
                inline=False
            )
    return emb

def dash_options(member: discord.Member, tab: str, page: int) -> List[discord.SelectOption]:
    items = dash_filter(member, tab)
    slice_, _, _ = dash_page(items, page)
    opts = []
    for it in slice_:
        if tab.startswith("todos"):
            label = f"{it['id']} · {it.get('title','—')[:60]}"
            opts.append(discord.SelectOption(label=label, description=f"todo {it.get('scope','public')}"[:100], value=str(it["id"])))
        else:
            dt = dt_from_iso(it["datetime"]).strftime("%d.%m.%Y %H:%M")
            label = f"{it['id']} · {it.get('title','—')[:50]}"
            opts.append(discord.SelectOption(label=label, description=dt, value=str(it["id"])))
    return opts

def find_todo(data: Dict[str, Any], tid: int) -> Optional[Dict[str, Any]]:
    return next((t for t in data["todos"] if int(t.get("id",-1)) == tid and not t.get("deleted")), None)

def find_event(data: Dict[str, Any], eid: int) -> Optional[Dict[str, Any]]:
    return next((e for e in data["events"] if int(e.get("id",-1)) == eid), None)

# ============================================================
# Modals (max 5 inputs)
# ============================================================
class AddTodoModal(discord.ui.Modal, title="Todo hinzufügen"):
    def __init__(self, parent: "DashboardView", picked_user_id: Optional[int], picked_role_id: Optional[int]):
        super().__init__(timeout=300)
        self.parent = parent
        self.picked_user_id = picked_user_id
        self.picked_role_id = picked_role_id

        self.titel = discord.ui.TextInput(label="Titel", required=True, max_length=120)
        self.desc = discord.ui.TextInput(label="Beschreibung (optional)", style=discord.TextStyle.paragraph, required=False, max_length=500)
        self.scope = discord.ui.TextInput(label="Scope (public/private/user/role)", default="public", required=False, max_length=10)
        self.due = discord.ui.TextInput(label="Fälligkeit (DD.MM.YYYY HH:MM) oder leer", required=False, max_length=20)
        hint = "Ausgewählt: "
        if picked_user_id:
            hint += "User"
        elif picked_role_id:
            hint += "Role"
        else:
            hint += "—"
        self.note = discord.ui.TextInput(label=f"Ziel aus Dropdown wird benutzt ({hint})", default="", required=False, max_length=1)

        # Trick: "note" ist nur zur Info; wird ignoriert. Muss aber <=5 bleiben.
        for x in (self.titel, self.desc, self.scope, self.due, self.note):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)

        scope = (self.scope.value or "public").strip().lower()
        if scope not in ("public","private","user","role"):
            scope = "public"

        au = ar = None
        if scope == "user":
            au = self.picked_user_id
        if scope == "role":
            ar = self.picked_role_id
        if scope in ("public","private"):
            au = ar = None

        # Validierung: wenn scope user/role -> Auswahl nötig
        if scope == "user" and not au:
            return await interaction.response.send_message("❌ Für scope **user** bitte im Dropdown einen User auswählen.", ephemeral=True)
        if scope == "role" and not ar:
            return await interaction.response.send_message("❌ Für scope **role** bitte im Dropdown eine Rolle auswählen.", ephemeral=True)

        due_iso = None
        raw = (self.due.value or "").strip()
        if raw:
            try:
                dpart, tpart = raw.split()
                due_iso = dt_to_iso(parse_date_time(dpart, tpart))
            except:
                return await interaction.response.send_message("❌ Fälligkeit ungültig. Beispiel: 10.03.2026 18:00", ephemeral=True)

        data = load_data()
        tid = new_todo_id(data)
        data["todos"].append({
            "id": tid,
            "title": self.titel.value.strip(),
            "description": (self.desc.value or "").strip(),
            "scope": scope,
            "assigned_user_id": au,
            "assigned_role_id": ar,
            "created_by": interaction.user.id,
            "created_at": dt_to_iso(now_berlin()),
            "due": due_iso,
            "done": False,
            "done_at": None,
            "deleted": False,
        })
        save_data(data)

        await interaction.response.send_message(f"✅ Todo **{tid}** erstellt.", ephemeral=True)
        await self.parent.refresh_message()

class AddEventModal(discord.ui.Modal, title="Termin hinzufügen"):
    def __init__(self, parent: "DashboardView", picked_dm_user_ids: List[int]):
        super().__init__(timeout=300)
        self.parent = parent
        self.picked_dm_user_ids = picked_dm_user_ids[:]  # aus Selector

        self.title_in = discord.ui.TextInput(label="Titel", required=True, max_length=120)
        self.dt_in = discord.ui.TextInput(label="Datum/Zeit (DD.MM.YYYY HH:MM)", required=True, max_length=20)
        self.rems_in = discord.ui.TextInput(label="Erinnerungen (z.B. 60,10,5)", default="30", required=False, max_length=60)
        self.rec_target = discord.ui.TextInput(label="Wiederholung+Target (z.B. 'none channel' oder 'weekly dm')", default="none channel", required=False, max_length=40)
        hint = f"{len(picked_dm_user_ids)} DM-Empfänger ausgewählt"
        self.note = discord.ui.TextInput(label=hint, default="", required=False, max_length=1)
        for x in (self.title_in, self.dt_in, self.rems_in, self.rec_target, self.note):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)

        title = self.title_in.value.strip()
        try:
            dpart, tpart = (self.dt_in.value or "").strip().split()
            dt = parse_date_time(dpart, tpart)
        except:
            return await interaction.response.send_message("❌ Datum/Zeit ungültig. Beispiel: 08.02.2026 12:00", ephemeral=True)

        rems = parse_reminders(self.rems_in.value or "30")

        rec = "none"
        target = "channel"
        rt = (self.rec_target.value or "").strip().lower()
        if rt:
            parts = [p for p in rt.split() if p]
            if len(parts) >= 1 and parts[0] in ("none","daily","weekly","monthly"):
                rec = parts[0]
            if len(parts) >= 2 and parts[1] in ("channel","dm"):
                target = parts[1]

        data = load_data()
        eid = new_event_id(data)

        if target == "dm":
            ids = set(self.picked_dm_user_ids)
            ids.add(interaction.user.id)  # immer Ersteller dazu
            tgt_obj = {"type":"dm", "user_ids": sorted(list(ids))}
        else:
            tgt_obj = {"type":"channel", "channel_id": ERINNERUNGS_CHANNEL_ID}

        data["events"].append({
            "id": eid,
            "title": title,
            "datetime": dt_to_iso(dt),
            "reminders": rems,
            "sent": [],
            "recurrence": rec,
            "cancelled": False,
            "target": tgt_obj,
            "created_by": interaction.user.id
        })
        save_data(data)

        if target == "channel":
            rem_txt = ", ".join(f"{m}m" for m in rems) if rems else "—"
            await send_channel_message(
                ERINNERUNGS_CHANNEL_ID,
                f"<@&{ROLLE_ID}> 📅 **Neuer Termin**\n📌 **{title}**\n🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n🔔 **Erinnerung:** {rem_txt} vorher\n🆔 ID: **{eid}**",
                delete_after=AUTO_DELETE_SECONDS
            )

        await interaction.response.send_message(f"✅ Termin **{eid}** erstellt ({target}).", ephemeral=True)
        await self.parent.refresh_message()

# ============================================================
# Dashboard Picker Views (User/Role Select)
# ============================================================
class TodoTargetSelect(discord.ui.MentionableSelect):
    def __init__(self):
        super().__init__(placeholder="Optional: User oder Rolle auswählen…", min_values=0, max_values=1)

class EventDMUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Optional: DM-Empfänger auswählen (mehrere)…", min_values=0, max_values=10)

class TodoCreatePickView(discord.ui.View):
    def __init__(self, parent: "DashboardView"):
        super().__init__(timeout=120)
        self.parent = parent
        self.picked_user_id: Optional[int] = None
        self.picked_role_id: Optional[int] = None
        self.select = TodoTargetSelect()
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)
        self.picked_user_id = None
        self.picked_role_id = None
        if self.select.values:
            v = self.select.values[0]
            if isinstance(v, discord.Member) or isinstance(v, discord.User):
                self.picked_user_id = v.id
            elif isinstance(v, discord.Role):
                self.picked_role_id = v.id
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Weiter (Modal öffnen)", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.parent.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)
        await interaction.response.send_modal(AddTodoModal(self.parent, self.picked_user_id, self.picked_role_id))

class EventCreatePickView(discord.ui.View):
    def __init__(self, parent: "DashboardView"):
        super().__init__(timeout=120)
        self.parent = parent
        self.picked_ids: List[int] = []
        self.select = EventDMUserSelect()
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.parent.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)
        self.picked_ids = [u.id for u in self.select.values] if self.select.values else []
        await interaction.response.defer(ephemeral=True)

    @discord.ui.button(label="Weiter (Modal öffnen)", style=discord.ButtonStyle.success)
    async def go(self, interaction: discord.Interaction, _):
        if interaction.user.id != self.parent.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)
        await interaction.response.send_modal(AddEventModal(self.parent, self.picked_ids))

# ============================================================
# Dashboard View (mit Buttons/Dropdown/Pagination)
# ============================================================
class DashSelect(discord.ui.Select):
    def __init__(self, view: "DashboardView"):
        self.v = view
        opts = dash_options(view.member, view.tab, view.page)
        if not opts:
            opts = [discord.SelectOption(label="Keine Einträge", value="0")]
        super().__init__(placeholder="Eintrag auswählen…", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if self.values and self.values[0] != "0":
            self.v.selected_id = int(self.values[0])
        await self.v.refresh(interaction)

class DashboardView(discord.ui.View):
    def __init__(self, member: discord.Member, message: discord.Message, tab: str="todos_open", page: int=0, selected_id: Optional[int]=None):
        super().__init__(timeout=900)
        self.member = member
        self.owner_id = member.id
        self.message = message
        self.tab = tab
        self.page = page
        self.selected_id = selected_id
        self.add_item(DashSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Das ist nicht dein Dashboard.", ephemeral=True)
            return False
        return True

    def rebuild(self) -> "DashboardView":
        return DashboardView(self.member, self.message, self.tab, self.page, self.selected_id)

    async def refresh(self, interaction: discord.Interaction):
        emb = dash_embed(self.member, self.tab, self.page, self.selected_id)
        await interaction.response.edit_message(embed=emb, view=self.rebuild())

    async def refresh_message(self):
        emb = dash_embed(self.member, self.tab, self.page, self.selected_id)
        await self.message.edit(embed=emb, view=self.rebuild())

    # Tabs
    @discord.ui.button(label="📝 Todos offen", style=discord.ButtonStyle.primary, row=1)
    async def tab_open(self, interaction: discord.Interaction, _):
        self.tab, self.page, self.selected_id = "todos_open", 0, None
        await self.refresh(interaction)

    @discord.ui.button(label="✅ Todos erledigt", style=discord.ButtonStyle.secondary, row=1)
    async def tab_done(self, interaction: discord.Interaction, _):
        self.tab, self.page, self.selected_id = "todos_done", 0, None
        await self.refresh(interaction)

    @discord.ui.button(label="📅 Termine aktiv", style=discord.ButtonStyle.success, row=1)
    async def tab_ea(self, interaction: discord.Interaction, _):
        self.tab, self.page, self.selected_id = "events_active", 0, None
        await self.refresh(interaction)

    @discord.ui.button(label="📦 Termine alle", style=discord.ButtonStyle.secondary, row=1)
    async def tab_all(self, interaction: discord.Interaction, _):
        self.tab, self.page, self.selected_id = "events_all", 0, None
        await self.refresh(interaction)

    # Pagination + Add + Refresh
    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def prev_page(self, interaction: discord.Interaction, _):
        self.page = max(0, self.page - 1)
        self.selected_id = None
        await self.refresh(interaction)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, _):
        self.page += 1
        self.selected_id = None
        await self.refresh(interaction)

    @discord.ui.button(label="➕ Todo", style=discord.ButtonStyle.success, row=2)
    async def add_todo(self, interaction: discord.Interaction, _):
        # Ephemeral Picker -> dann Modal
        await interaction.response.send_message(
            "Wähle optional **User oder Rolle** (für scope=user/role) und klicke **Weiter**.",
            ephemeral=True,
            view=TodoCreatePickView(self)
        )

    @discord.ui.button(label="➕ Termin", style=discord.ButtonStyle.success, row=2)
    async def add_event(self, interaction: discord.Interaction, _):
        await interaction.response.send_message(
            "Wähle optional **DM-Empfänger** (für target=dm) und klicke **Weiter**.",
            ephemeral=True,
            view=EventCreatePickView(self)
        )

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def ref(self, interaction: discord.Interaction, _):
        await self.refresh(interaction)

    # Todo actions
    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=3)
    async def todo_done_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        data = load_data()
        todo = find_todo(data, self.selected_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, self.member):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        todo["done"] = True
        todo["done_at"] = dt_to_iso(now_berlin())
        save_data(data)
        await interaction.response.send_message(f"✅ Todo {self.selected_id} erledigt.", ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(label="↩️ Undo", style=discord.ButtonStyle.primary, row=3)
    async def todo_undo_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        data = load_data()
        todo = find_todo(data, self.selected_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, self.member):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        todo["done"] = False
        todo["done_at"] = None
        save_data(data)
        await interaction.response.send_message(f"↩️ Todo {self.selected_id} wieder offen.", ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=3)
    async def todo_del_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        data = load_data()
        todo = find_todo(data, self.selected_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, self.member):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        todo["deleted"] = True
        save_data(data)
        await interaction.response.send_message(f"🗑️ Todo {self.selected_id} gelöscht.", ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(label="✏️ Edit Todo", style=discord.ButtonStyle.secondary, row=3)
    async def todo_edit_btn(self, interaction: discord.Interaction, _):
        return await interaction.response.send_message("ℹ️ Todo-Edit per **/todo_edit** (mit User/Role Auswahl über Discord UI).", ephemeral=True)

    # Event actions
    @discord.ui.button(label="❌ Absagen", style=discord.ButtonStyle.danger, row=4)
    async def event_cancel_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("events") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst einen Termin auswählen.", ephemeral=True)
        data = load_data()
        ev = find_event(data, self.selected_id)
        if not ev:
            return await interaction.response.send_message("❌ Termin nicht gefunden.", ephemeral=True)
        ev["cancelled"] = True
        save_data(data)
        await interaction.response.send_message(f"❌ Termin {self.selected_id} abgesagt.", ephemeral=True)
        await self.refresh_message()

    @discord.ui.button(label="✏️ Edit Termin", style=discord.ButtonStyle.secondary, row=4)
    async def event_edit_btn(self, interaction: discord.Interaction, _):
        return await interaction.response.send_message("ℹ️ Termin-Edit per **/termin_edit** (Datum/Zeit/Reminder/Wiederholung).", ephemeral=True)

# =========================
# /dashboard (wird nach 15min gelöscht)
# =========================
@bot.tree.command(name="dashboard", description="Interaktives Dashboard (Todos + Termine)")
async def dashboard_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Bitte im Server ausführen.", ephemeral=True)

    ch = bot.get_channel(DASHBOARD_CHANNEL_ID) if DASHBOARD_CHANNEL_ID else interaction.channel
    if not ch:
        ch = interaction.channel

    await interaction.response.send_message("✅ Dashboard wird gepostet (nur du kannst es bedienen).", ephemeral=True)

    emb = dash_embed(interaction.user, "todos_open", 0, None)
    msg = await ch.send(embed=emb, delete_after=AUTO_DELETE_SECONDS)
    view = DashboardView(interaction.user, msg, "todos_open", 0, None)
    await msg.edit(view=view)

# =========================
# Start
# =========================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
