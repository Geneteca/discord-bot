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

TZ = ZoneInfo("Europe/Berlin")
DATA_FILE = "data.json"
AUTO_DELETE_SECONDS = 900
CHECK_INTERVAL_SECONDS = 20
PAGE_SIZE = 6

CHOICES_REC = [
    app_commands.Choice(name="none", value="none"),
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="monthly", value="monthly"),
]

# =========================
# Helpers (Zeit/Daten)
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

async def send_channel_message(channel_id: int, content: str):
    ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await ch.send(content, delete_after=AUTO_DELETE_SECONDS)

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
# Reminder Loop (Termine)
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
# /help /ping
# =========================
@bot.tree.command(name="help", description="Übersicht aller Commands")
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**ℹ️ Allgemein**\n"
        "/ping – Bot-Status & Latenz\n"
        "/help – Diese Übersicht\n"
        "/dashboard – Dashboard (Todos + Termine)\n"
    )
    await interaction.response.send_message(text, ephemeral=True)

@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)} ms`", ephemeral=True)

# ============================================================
# DASHBOARD HELPERS
# ============================================================
def _dash_filter(member: discord.Member, tab: str) -> List[Dict[str, Any]]:
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

def _page(items: List[Dict[str, Any]], page: int) -> Tuple[List[Dict[str, Any]], int, int]:
    total = len(items)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * PAGE_SIZE
    return items[start:start+PAGE_SIZE], page, pages

def _embed(member: discord.Member, tab: str, page: int, selected: Optional[int]) -> discord.Embed:
    items = _dash_filter(member, tab)
    slice_, page, pages = _page(items, page)

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
            due = fmt_due(t.get("due"))
            desc = (t.get("description") or "—")
            emb.add_field(
                name=f"{status} ID {t['id']} · {t.get('title','—')} ({scope}){due}",
                value=(desc[:180] + ("…" if len(desc) > 180 else "")),
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

def _options(member: discord.Member, tab: str, page: int) -> List[discord.SelectOption]:
    items = _dash_filter(member, tab)
    slice_, _, _ = _page(items, page)
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

def _find_todo(data: Dict[str, Any], tid: int) -> Optional[Dict[str, Any]]:
    return next((t for t in data["todos"] if int(t.get("id",-1)) == tid and not t.get("deleted")), None)

def _find_event(data: Dict[str, Any], eid: int) -> Optional[Dict[str, Any]]:
    return next((e for e in data["events"] if int(e.get("id",-1)) == eid), None)

# ============================================================
# MODALS (Add/Edit)
# ============================================================
class AddTodoModal(discord.ui.Modal, title="Todo hinzufügen"):
    def __init__(self, owner_id: int, dash_msg_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.dash_msg_id = dash_msg_id

        self.titel = discord.ui.TextInput(label="Titel", required=True, max_length=120)
        self.desc = discord.ui.TextInput(label="Beschreibung (optional)", style=discord.TextStyle.paragraph, required=False, max_length=500)
        self.scope = discord.ui.TextInput(label="Scope (public/private/user/role)", default="public", required=False, max_length=10)
        self.user_id = discord.ui.TextInput(label="User ID (nur scope=user)", required=False, max_length=30)
        self.role_id = discord.ui.TextInput(label="Role ID (nur scope=role)", required=False, max_length=30)
        self.due = discord.ui.TextInput(label="Fälligkeit (DD.MM.YYYY HH:MM) oder leer", required=False, max_length=20)

        for x in (self.titel, self.desc, self.scope, self.user_id, self.role_id, self.due):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)

        scope = (self.scope.value or "public").strip().lower()
        if scope not in ("public","private","user","role"):
            scope = "public"

        au = ar = None
        if scope == "user":
            try: au = int((self.user_id.value or "").strip())
            except: au = None
        if scope == "role":
            try: ar = int((self.role_id.value or "").strip())
            except: ar = None
        if scope in ("public","private"):
            au = ar = None

        due_iso = None
        due_raw = (self.due.value or "").strip()
        if due_raw:
            try:
                dpart, tpart = due_raw.split()
                due_iso = dt_to_iso(parse_date_time(dpart, tpart))
            except:
                due_iso = None

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

        # Dashboard automatisch updaten:
        await refresh_dashboard_message(interaction, self.dash_msg_id)

class AddEventModal(discord.ui.Modal, title="Termin hinzufügen"):
    def __init__(self, owner_id: int, dash_msg_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.dash_msg_id = dash_msg_id

        self.title_in = discord.ui.TextInput(label="Titel", required=True, max_length=120)
        self.dt_in = discord.ui.TextInput(label="Datum/Zeit (DD.MM.YYYY HH:MM)", required=True, max_length=20)
        self.rems_in = discord.ui.TextInput(label="Erinnerungen (z.B. 60,10,5)", default="30", required=False, max_length=60)
        self.rec_in = discord.ui.TextInput(label="Wiederholung (none/daily/weekly/monthly)", default="none", required=False, max_length=10)
        self.target_in = discord.ui.TextInput(label="Target (channel/dm)", default="channel", required=False, max_length=10)
        self.dm_user_ids = discord.ui.TextInput(label="DM User IDs (kommagetrennt; nur target=dm)", required=False, max_length=200)

        for x in (self.title_in, self.dt_in, self.rems_in, self.rec_in, self.target_in, self.dm_user_ids):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)

        title = self.title_in.value.strip()
        dt_raw = (self.dt_in.value or "").strip()
        try:
            dpart, tpart = dt_raw.split()
            dt = parse_date_time(dpart, tpart)
        except:
            return await interaction.response.send_message("❌ Datum/Zeit ungültig. Beispiel: 08.02.2026 12:00", ephemeral=True)

        rems = parse_reminders(self.rems_in.value or "30")
        rec = (self.rec_in.value or "none").strip().lower()
        if rec not in ("none","daily","weekly","monthly"):
            rec = "none"

        target = (self.target_in.value or "channel").strip().lower()
        if target not in ("channel","dm"):
            target = "channel"

        data = load_data()
        eid = new_event_id(data)

        if target == "dm":
            ids = {interaction.user.id}
            raw = (self.dm_user_ids.value or "").strip()
            if raw:
                for p in raw.split(","):
                    p = p.strip()
                    if not p:
                        continue
                    try:
                        ids.add(int(p))
                    except:
                        pass
            tgt_obj = {"type": "dm", "user_ids": sorted(list(ids))}
        else:
            tgt_obj = {"type": "channel", "channel_id": ERINNERUNGS_CHANNEL_ID}

        data["events"].append({
            "id": eid,
            "title": title,
            "datetime": dt_to_iso(dt),
            "reminders": rems,
            "sent": [],
            "recurrence": rec,
            "cancelled": False,
            "target": tgt_obj,
            "created_by": interaction.user.id,
        })
        save_data(data)

        if target == "channel":
            rem_txt = ", ".join(f"{m}m" for m in rems) if rems else "—"
            await send_channel_message(
                ERINNERUNGS_CHANNEL_ID,
                f"<@&{ROLLE_ID}> 📅 **Neuer Termin**\n📌 **{title}**\n🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n🔔 **Erinnerung:** {rem_txt} vorher\n🆔 ID: **{eid}**"
            )

        await interaction.response.send_message(f"✅ Termin **{eid}** erstellt ({target}).", ephemeral=True)
        await refresh_dashboard_message(interaction, self.dash_msg_id)

class TodoEditModal(discord.ui.Modal, title="Todo bearbeiten"):
    def __init__(self, owner_id: int, dash_msg_id: int, todo_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.dash_msg_id = dash_msg_id
        self.todo_id = todo_id

        data = load_data()
        t = next((x for x in data["todos"] if int(x.get("id",-1)) == todo_id and not x.get("deleted")), {}) or {}

        self.title_in = discord.ui.TextInput(label="Titel", default=t.get("title",""), required=False, max_length=120)
        self.desc_in = discord.ui.TextInput(label="Beschreibung", default=t.get("description",""), style=discord.TextStyle.paragraph, required=False, max_length=500)
        self.scope_in = discord.ui.TextInput(label="Scope (public/private/user/role)", default=t.get("scope","public"), required=False, max_length=10)
        self.user_id_in = discord.ui.TextInput(label="Assigned User ID (scope=user)", default=str(t.get("assigned_user_id") or ""), required=False, max_length=30)
        self.role_id_in = discord.ui.TextInput(label="Assigned Role ID (scope=role)", default=str(t.get("assigned_role_id") or ""), required=False, max_length=30)

        due_default = ""
        if t.get("due"):
            try:
                d = dt_from_iso(t["due"])
                due_default = d.strftime("%d.%m.%Y %H:%M")
            except:
                pass
        self.due_in = discord.ui.TextInput(label="Fälligkeit (DD.MM.YYYY HH:MM) oder leer", default=due_default, required=False, max_length=20)

        for x in (self.title_in, self.desc_in, self.scope_in, self.user_id_in, self.role_id_in, self.due_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)
        if not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Bitte im Server ausführen.", ephemeral=True)

        data = load_data()
        todo = _find_todo(data, self.todo_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, interaction.user):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)

        if self.title_in.value.strip():
            todo["title"] = self.title_in.value.strip()
        todo["description"] = (self.desc_in.value or "").strip()

        scope = (self.scope_in.value or "").strip().lower()
        if scope not in ("public","private","user","role"):
            scope = todo.get("scope","public")

        todo["scope"] = scope
        if scope in ("public","private"):
            todo["assigned_user_id"] = None
            todo["assigned_role_id"] = None
        elif scope == "user":
            try:
                todo["assigned_user_id"] = int((self.user_id_in.value or "").strip())
                todo["assigned_role_id"] = None
            except:
                pass
        elif scope == "role":
            try:
                todo["assigned_role_id"] = int((self.role_id_in.value or "").strip())
                todo["assigned_user_id"] = None
            except:
                pass

        due_raw = (self.due_in.value or "").strip()
        if due_raw == "":
            todo["due"] = None
        else:
            try:
                dpart, tpart = due_raw.split()
                todo["due"] = dt_to_iso(parse_date_time(dpart, tpart))
            except:
                pass

        save_data(data)
        await interaction.response.send_message(f"✅ Todo {self.todo_id} gespeichert.", ephemeral=True)
        await refresh_dashboard_message(interaction, self.dash_msg_id)

class EventEditModal(discord.ui.Modal, title="Termin bearbeiten"):
    def __init__(self, owner_id: int, dash_msg_id: int, event_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.dash_msg_id = dash_msg_id
        self.event_id = event_id

        data = load_data()
        e = _find_event(data, event_id) or {}

        dt_default = ""
        if e.get("datetime"):
            try:
                dt_default = dt_from_iso(e["datetime"]).strftime("%d.%m.%Y %H:%M")
            except:
                pass

        self.title_in = discord.ui.TextInput(label="Titel", default=e.get("title",""), required=False, max_length=120)
        self.dt_in = discord.ui.TextInput(label="Datum/Zeit (DD.MM.YYYY HH:MM)", default=dt_default, required=False, max_length=20)
        self.rems_in = discord.ui.TextInput(label="Erinnerungen (z.B. 60,10,5)", default=",".join(str(x) for x in e.get("reminders", [])), required=False, max_length=60)
        self.rec_in = discord.ui.TextInput(label="Wiederholung (none/daily/weekly/monthly)", default=e.get("recurrence","none"), required=False, max_length=10)

        for x in (self.title_in, self.dt_in, self.rems_in, self.rec_in):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True)

        data = load_data()
        ev = _find_event(data, self.event_id)
        if not ev:
            return await interaction.response.send_message("❌ Termin nicht gefunden.", ephemeral=True)

        if self.title_in.value.strip():
            ev["title"] = self.title_in.value.strip()

        dt_raw = (self.dt_in.value or "").strip()
        if dt_raw:
            try:
                dpart, tpart = dt_raw.split()
                ev["datetime"] = dt_to_iso(parse_date_time(dpart, tpart))
                ev["sent"] = []
            except:
                pass

        rem_raw = (self.rems_in.value or "").strip()
        if rem_raw != "":
            try:
                ev["reminders"] = parse_reminders(rem_raw)
                ev["sent"] = []
            except:
                pass

        rec = (self.rec_in.value or "").strip().lower()
        if rec in ("none","daily","weekly","monthly"):
            ev["recurrence"] = rec

        save_data(data)
        await interaction.response.send_message(f"✅ Termin {self.event_id} gespeichert.", ephemeral=True)
        await refresh_dashboard_message(interaction, self.dash_msg_id)

# ============================================================
# Dashboard: View / Select / Auto-refresh
# ============================================================
class DashSelect(discord.ui.Select):
    def __init__(self, view: "DashboardView"):
        self.dview = view
        opts = _options(view.member, view.tab, view.page)
        if not opts:
            opts = [discord.SelectOption(label="Keine Einträge auf dieser Seite", value="0")]
        super().__init__(placeholder="Eintrag auswählen…", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if self.values and self.values[0] != "0":
            self.dview.selected_id = int(self.values[0])
        await self.dview.refresh(interaction)

class DashboardView(discord.ui.View):
    def __init__(self, member: discord.Member, tab: str="todos_open", page: int=0, selected_id: Optional[int]=None, msg_id: Optional[int]=None):
        super().__init__(timeout=600)
        self.member = member
        self.owner_id = member.id
        self.tab = tab
        self.page = page
        self.selected_id = selected_id
        self.msg_id = msg_id
        self.add_item(DashSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Das ist nicht dein Dashboard.", ephemeral=True)
            return False
        return True

    def rebuild(self) -> "DashboardView":
        return DashboardView(self.member, self.tab, self.page, self.selected_id, self.msg_id)

    async def refresh(self, interaction: discord.Interaction):
        emb = _embed(self.member, self.tab, self.page, self.selected_id)
        await interaction.response.edit_message(embed=emb, view=self.rebuild())

    async def refresh_silent(self, message: discord.Message):
        emb = _embed(self.member, self.tab, self.page, self.selected_id)
        await message.edit(embed=emb, view=self.rebuild())

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

    # Pagination
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

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def ref(self, interaction: discord.Interaction, _):
        await self.refresh(interaction)

    # Add
    @discord.ui.button(label="➕ Todo", style=discord.ButtonStyle.success, row=2)
    async def add_todo(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(AddTodoModal(self.owner_id, self.msg_id or 0))

    @discord.ui.button(label="➕ Termin", style=discord.ButtonStyle.success, row=2)
    async def add_event(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(AddEventModal(self.owner_id, self.msg_id or 0))

    # Todo actions
    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=3)
    async def todo_done_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        data = load_data()
        todo = _find_todo(data, self.selected_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, self.member):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        todo["done"] = True
        todo["done_at"] = dt_to_iso(now_berlin())
        save_data(data)
        await interaction.response.send_message(f"✅ Todo {self.selected_id} erledigt.", ephemeral=True)
        await refresh_dashboard_message(interaction, self.msg_id or 0)

    @discord.ui.button(label="↩️ Undo", style=discord.ButtonStyle.primary, row=3)
    async def todo_undo_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        data = load_data()
        todo = _find_todo(data, self.selected_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, self.member):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        todo["done"] = False
        todo["done_at"] = None
        save_data(data)
        await interaction.response.send_message(f"↩️ Todo {self.selected_id} wieder offen.", ephemeral=True)
        await refresh_dashboard_message(interaction, self.msg_id or 0)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=3)
    async def todo_del_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        data = load_data()
        todo = _find_todo(data, self.selected_id)
        if not todo:
            return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not can_modify_todo(todo, self.member):
            return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        todo["deleted"] = True
        save_data(data)
        await interaction.response.send_message(f"🗑️ Todo {self.selected_id} gelöscht.", ephemeral=True)
        await refresh_dashboard_message(interaction, self.msg_id or 0)

    @discord.ui.button(label="✏️ Edit Todo", style=discord.ButtonStyle.secondary, row=3)
    async def todo_edit_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        await interaction.response.send_modal(TodoEditModal(self.owner_id, self.msg_id or 0, self.selected_id))

    # Event actions
    @discord.ui.button(label="❌ Absagen", style=discord.ButtonStyle.danger, row=4)
    async def event_cancel_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("events") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst einen Termin auswählen.", ephemeral=True)
        data = load_data()
        ev = _find_event(data, self.selected_id)
        if not ev:
            return await interaction.response.send_message("❌ Termin nicht gefunden.", ephemeral=True)
        ev["cancelled"] = True
        save_data(data)
        await interaction.response.send_message(f"❌ Termin {self.selected_id} abgesagt.", ephemeral=True)
        await refresh_dashboard_message(interaction, self.msg_id or 0)

    @discord.ui.button(label="✏️ Edit Termin", style=discord.ButtonStyle.secondary, row=4)
    async def event_edit_btn(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("events") or not self.selected_id:
            return await interaction.response.send_message("❌ Erst einen Termin auswählen.", ephemeral=True)
        await interaction.response.send_modal(EventEditModal(self.owner_id, self.msg_id or 0, self.selected_id))

async def refresh_dashboard_message(interaction: discord.Interaction, msg_id: int):
    # Dashboard ist ephemeral => wir können es über interaction.message (bei Buttons) oder fetch über response-message nicht immer.
    # Trick: wir benutzen interaction.channel.history und suchen Message-ID im aktuellen Channel (ephemeral nicht fetchbar).
    # Deshalb: wir refreshen den Dashboard-View nur zuverlässig, wenn wir message-Objekt haben.
    # Lösung: Bei /dashboard speichern wir die Message-ID und nutzen interaction.message wenn möglich.

    try:
        if interaction.message and interaction.message.id == msg_id:
            # direkte Edit möglich (Buttons/Select)
            view = interaction.message.components  # not used; rebuild below
            # Rebuild view with same state: not accessible here
            # We'll do a lightweight approach: re-send a "Refresh" to force user click? -> not needed; use edit via new View stored in custom_id
            pass
    except:
        pass

    # Robust: Wenn wir die Message direkt in Hand haben, nehmen wir die:
    if interaction.message and (msg_id == 0 or interaction.message.id == msg_id):
        # reconstruct state from current view object if possible (not accessible), so we just rebuild from defaults
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member:
            # best-effort: keep tab from existing view if present
            tab = "todos_open"
            page = 0
            selected = None
            if isinstance(interaction.message, discord.Message) and interaction.message.components:
                # We can't read internal state from components; so we keep current "tab" by parsing embed title
                try:
                    title = interaction.message.embeds[0].title or ""
                    if "Todos – erledigt" in title: tab = "todos_done"
                    elif "Termine – aktiv" in title: tab = "events_active"
                    elif "Termine – alle" in title: tab = "events_all"
                    else: tab = "todos_open"
                except:
                    tab = "todos_open"
            emb = _embed(member, tab, page, selected)
            new_view = DashboardView(member, tab, page, selected, interaction.message.id)
            await interaction.message.edit(embed=emb, view=new_view)
        return

    # Fallback: wir können ephemerals nicht “fetchen”. Daher senden wir einfach eine Hinweis-Antwort.
    # (Dashboard aktualisiert sich spätestens beim nächsten Klick / Öffnen.)
    try:
        await interaction.followup.send("ℹ️ Dashboard aktualisiert sich beim nächsten Klick/Refresh.", ephemeral=True)
    except:
        pass

# =========================
# /dashboard
# =========================
@bot.tree.command(name="dashboard", description="Interaktives Dashboard (Todos + Termine)")
async def dashboard_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Bitte im Server ausführen.", ephemeral=True)

    emb = _embed(interaction.user, "todos_open", 0, None)
    view = DashboardView(interaction.user, "todos_open", 0, None, msg_id=0)
    await interaction.response.send_message(embed=emb, view=view, ephemeral=True)

# =========================
# Start
# =========================
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
