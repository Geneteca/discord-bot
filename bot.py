import os, json, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Set

import discord
from discord.ext import commands
from discord import app_commands

# ===== ENV =====
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])
CLEAN_GLOBAL_COMMANDS = os.environ.get("CLEAN_GLOBAL_COMMANDS", "0").strip() == "1"

TZ = ZoneInfo("Europe/Berlin")
DATA_FILE = "data.json"
AUTO_DELETE = 900
TICK = 20

REC_CHOICES = [
    app_commands.Choice(name="none", value="none"),
    app_commands.Choice(name="daily", value="daily"),
    app_commands.Choice(name="weekly", value="weekly"),
    app_commands.Choice(name="monthly", value="monthly"),
]

# ===== DATA =====
def load() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("events", [])
    d.setdefault("next_event_id", 1)
    d.setdefault("todos", [])
    d.setdefault("next_todo_id", 1)
    return d

def save(d: Dict[str, Any]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def next_id(d: Dict[str, Any], key: str) -> int:
    d[key] = int(d.get(key, 1))
    nid = d[key]
    d[key] += 1
    return nid

# ===== TIME / PARSE =====
def now() -> datetime:
    return datetime.now(tz=TZ)

def iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(TZ).isoformat()

def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(TZ)

def parse_dt(d: str, t: str) -> datetime:
    return datetime.strptime(f"{d} {t}", "%d.%m.%Y %H:%M").replace(tzinfo=TZ)

def parse_rems(s: str) -> List[int]:
    s = (s or "").strip().lower()
    if not s:
        return []
    out = []
    for p in [x.strip() for x in s.split(",") if x.strip()]:
        if p.endswith("m"): out.append(int(p[:-1]))
        elif p.endswith("h"): out.append(int(p[:-1]) * 60)
        elif p.endswith("d"): out.append(int(p[:-1]) * 1440)
        else: out.append(int(p))
    return sorted({x for x in out if x >= 0}, reverse=True)

def add_month(dt: datetime) -> datetime:
    y, m = dt.year, dt.month + 1
    if m == 13: y, m = y + 1, 1
    next_month = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=dt.tzinfo)
    last_day = (next_month - timedelta(days=1)).day
    return dt.replace(year=y, month=m, day=min(dt.day, last_day))

def next_occ(dt: datetime, rec: str) -> datetime:
    rec = (rec or "none").lower()
    if rec == "daily": return dt + timedelta(days=1)
    if rec == "weekly": return dt + timedelta(weeks=1)
    if rec == "monthly": return add_month(dt)
    return dt

# ===== DISCORD =====
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def ch_send(cid: int, content: str):
    ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
    await ch.send(content, delete_after=AUTO_DELETE)

async def dm_send(uid: int, content: str):
    user = bot.get_user(uid) or await bot.fetch_user(uid)
    await user.send(content)

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)

async def do_sync():
    guild = discord.Object(id=GUILD_ID)
    if CLEAN_GLOBAL_COMMANDS:
        print("🧹 Lösche globale Slash-Commands …", flush=True)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    remote = await bot.tree.fetch_commands(guild=guild)
    print(f"✅ Slash-Commands synced to guild {GUILD_ID}", flush=True)
    print(f"📌 Remote Commands (Guild): {[c.name for c in remote]}", flush=True)

@bot.event
async def setup_hook():
    await do_sync()
    bot.loop.create_task(reminder_loop())

# ===== REMINDER LOOP (Termine) =====
def reminder_text(title: str, dt: datetime, m: int) -> str:
    return (f"🔔 **Erinnerung** ({m} min vorher)\n"
            f"📌 **{title}**\n"
            f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)")

async def reminder_loop():
    await bot.wait_until_ready()
    print("⏰ Reminder-Loop aktiv", flush=True)
    while not bot.is_closed():
        try:
            d, changed = load(), False
            n = now()
            for ev in d["events"]:
                if ev.get("cancelled"): 
                    continue
                dt = from_iso(ev["datetime"])
                rems = [int(x) for x in ev.get("reminders", [])]
                sent = set(int(x) for x in ev.get("sent", []))

                for m in rems:
                    if m in sent:
                        continue
                    if n >= (dt - timedelta(minutes=m)) and n < dt + timedelta(hours=24):
                        msg = reminder_text(ev["title"], dt, m)
                        if ev["target"]["type"] == "channel":
                            await ch_send(ev["target"]["channel_id"], f"<@&{ROLLE_ID}> {msg}")
                        else:
                            for uid in ev["target"]["user_ids"]:
                                await dm_send(uid, msg)
                        sent.add(m)
                        ev["sent"] = sorted(sent, reverse=True)
                        changed = True

                if n >= dt:
                    rec = (ev.get("recurrence") or "none").lower()
                    if rec != "none":
                        ev["datetime"] = iso(next_occ(dt, rec))
                        ev["sent"] = []
                    else:
                        ev["cancelled"] = True
                    changed = True

            if changed:
                save(d)
        except Exception as e:
            print(f"❌ Loop-Fehler: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(TICK)

# ===== TODO Rechte =====
def role_ids(member: discord.Member) -> Set[int]:
    return {r.id for r in getattr(member, "roles", [])}

def todo_relevant(t: Dict[str, Any], m: discord.Member) -> bool:
    if t.get("deleted"): return False
    sc = t.get("scope", "public")
    if sc == "public": return True
    if sc == "private": return int(t.get("created_by", 0)) == m.id
    if sc == "user": return int(t.get("assigned_user_id", 0)) == m.id or int(t.get("created_by", 0)) == m.id
    if sc == "role": return int(t.get("assigned_role_id", 0)) in role_ids(m) or int(t.get("created_by", 0)) == m.id
    return False

def todo_can_edit(t: Dict[str, Any], m: discord.Member) -> bool:
    if int(t.get("created_by", 0)) == m.id: return True
    if t.get("scope") == "user" and int(t.get("assigned_user_id", 0)) == m.id: return True
    return m.guild_permissions.manage_guild

def fmt_due(due: Optional[str]) -> str:
    if not due: return ""
    try:
        return " · fällig: " + from_iso(due).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""

# ===== /help /ping =====
@bot.tree.command(name="help", description="Übersicht aller Commands")
async def help_cmd(interaction: discord.Interaction):
    msg = (
        "**ℹ️ Allgemein**\n"
        "/ping – Bot-Status & Latenz\n"
        "/help – Diese Übersicht\n\n"
        "**📅 Termine**\n"
        "/termin – Öffentlichen Termin im Channel erstellen (mit Rollen-Ping)\n"
        "/ptermin – Privaten Termin erstellen (DM an dich + ausgewählte Personen)\n"
        "/termine – Aktive Termine\n"
        "/termine_all – Alle Termine\n"
        "/termin_edit – Termin bearbeiten (ID)\n"
        "/termin_absagen – Termin absagen (ID)\n\n"
        "**📝 Todos**\n"
        "/todo – Todo erstellen (öffentlich/privat/user/rolle)\n"
        "/todos – Offene Todos\n"
        "/oldtodos – Erledigte Todos\n"
        "/todo_done – Abhaken\n"
        "/todo_undo – Wieder öffnen\n"
        "/todo_edit – Bearbeiten\n"
        "/todo_delete – Löschen\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! Latenz: `{round(bot.latency*1000)} ms`", ephemeral=True)

# ===== Termine =====
@bot.tree.command(name="termin", description="Öffentlicher Termin (Channel) mit Rollen-Ping")
@app_commands.describe(datum="DD.MM.YYYY", uhrzeit="HH:MM", titel="Titel", erinnerung="z.B. 60,10,5", wiederholung="none/daily/weekly/monthly")
@app_commands.choices(wiederholung=REC_CHOICES)
async def termin_cmd(interaction: discord.Interaction, datum: str, uhrzeit: str, titel: str, erinnerung: str="30", wiederholung: str="none"):
    await interaction.response.defer(ephemeral=True)
    try:
        dt = parse_dt(datum, uhrzeit)
    except Exception:
        return await interaction.followup.send("❌ Datum/Uhrzeit ungültig. Beispiel: 08.02.2026 & 12:00", ephemeral=True)

    d = load()
    eid = next_id(d, "next_event_id")
    rems = parse_rems(erinnerung)
    d["events"].append({
        "id": eid, "title": titel.strip(), "datetime": iso(dt),
        "reminders": rems, "sent": [], "recurrence": wiederholung,
        "cancelled": False, "target": {"type":"channel","channel_id":ERINNERUNGS_CHANNEL_ID},
        "created_by": interaction.user.id
    })
    save(d)

    rem_txt = ", ".join(f"{m}m" for m in rems) if rems else "—"
    announce = (f"<@&{ROLLE_ID}> 📅 **Neuer Termin**\n"
                f"📌 **{titel}**\n"
                f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n"
                f"🔔 **Erinnerung:** {rem_txt} vorher\n"
                f"🆔 ID: **{eid}**")
    await ch_send(ERINNERUNGS_CHANNEL_ID, announce)
    await interaction.followup.send(f"✅ Termin gespeichert. ID: **{eid}**", ephemeral=True)

@bot.tree.command(name="ptermin", description="Privater Termin per DM (ohne Rollen-Ping)")
@app_commands.describe(datum="DD.MM.YYYY", uhrzeit="HH:MM", titel="Titel", erinnerung="z.B. 60,10,5", wiederholung="none/daily/weekly/monthly",
                      person1="Optional", person2="Optional", person3="Optional", person4="Optional", person5="Optional")
@app_commands.choices(wiederholung=REC_CHOICES)
async def ptermin_cmd(
    interaction: discord.Interaction, datum: str, uhrzeit: str, titel: str,
    erinnerung: str="30", wiederholung: str="none",
    person1: Optional[discord.Member]=None, person2: Optional[discord.Member]=None, person3: Optional[discord.Member]=None,
    person4: Optional[discord.Member]=None, person5: Optional[discord.Member]=None
):
    await interaction.response.defer(ephemeral=True)
    try:
        dt = parse_dt(datum, uhrzeit)
    except Exception:
        return await interaction.followup.send("❌ Datum/Uhrzeit ungültig. Beispiel: 08.02.2026 & 12:00", ephemeral=True)

    ids = {interaction.user.id}
    for p in (person1, person2, person3, person4, person5):
        if p: ids.add(p.id)

    d = load()
    eid = next_id(d, "next_event_id")
    d["events"].append({
        "id": eid, "title": titel.strip(), "datetime": iso(dt),
        "reminders": parse_rems(erinnerung), "sent": [],
        "recurrence": wiederholung, "cancelled": False,
        "target": {"type":"dm","user_ids": sorted(ids)},
        "created_by": interaction.user.id
    })
    save(d)
    await interaction.followup.send(f"✅ Privater Termin gespeichert. ID: **{eid}**. Empfänger: **{len(ids)}**", ephemeral=True)

@bot.tree.command(name="termine", description="Zeigt nur aktive (zukünftige) Termine")
async def termine_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    d, n = load(), now()
    evs = [e for e in d["events"] if not e.get("cancelled") and from_iso(e["datetime"]) >= n]
    evs.sort(key=lambda e: from_iso(e["datetime"]))
    if not evs:
        return await interaction.followup.send("📭 Keine aktiven Termine.", ephemeral=True)

    lines = []
    for e in evs[:25]:
        dt = from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termine_all", description="Zeigt alle Termine (inkl. alte/abgesagte)")
async def termine_all_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    d = load()
    evs = sorted(d["events"], key=lambda e: from_iso(e["datetime"]))
    if not evs:
        return await interaction.followup.send("📭 Keine Termine gespeichert.", ephemeral=True)

    lines = []
    for e in evs[:25]:
        dt = from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        status = "abgesagt/erledigt" if e.get("cancelled") else "aktiv"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']} · {status}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termin_absagen", description="Sagt einen Termin ab (per ID)")
@app_commands.describe(termin_id="ID aus /termine oder /termine_all")
async def termin_absagen_cmd(interaction: discord.Interaction, termin_id: int):
    await interaction.response.defer(ephemeral=True)
    d = load()
    for e in d["events"]:
        if int(e.get("id",-1)) == int(termin_id) and not e.get("cancelled"):
            e["cancelled"] = True
            save(d)
            return await interaction.followup.send(f"❌ Termin **{termin_id}** abgesagt.", ephemeral=True)
    await interaction.followup.send("❌ Termin-ID nicht gefunden oder schon abgesagt.", ephemeral=True)

@bot.tree.command(name="termin_edit", description="Bearbeitet einen Termin (per ID)")
@app_commands.describe(termin_id="ID", datum="Optional DD.MM.YYYY", uhrzeit="Optional HH:MM", titel="Optional", erinnerung="Optional z.B. 120,30,10", wiederholung="Optional")
@app_commands.choices(wiederholung=REC_CHOICES)
async def termin_edit_cmd(
    interaction: discord.Interaction, termin_id: int,
    datum: Optional[str]=None, uhrzeit: Optional[str]=None, titel: Optional[str]=None,
    erinnerung: Optional[str]=None, wiederholung: Optional[str]=None
):
    await interaction.response.defer(ephemeral=True)
    d = load()
    ev = next((e for e in d["events"] if int(e.get("id",-1)) == int(termin_id) and not e.get("cancelled")), None)
    if not ev:
        return await interaction.followup.send("❌ Termin-ID nicht gefunden.", ephemeral=True)

    if titel and titel.strip(): ev["title"] = titel.strip()
    if erinnerung is not None:
        ev["reminders"] = parse_rems(erinnerung)
        ev["sent"] = []
    if wiederholung is not None:
        ev["recurrence"] = wiederholung

    if datum is not None or uhrzeit is not None:
        cur = from_iso(ev["datetime"])
        dstr = datum if datum is not None else cur.strftime("%d.%m.%Y")
        tstr = uhrzeit if uhrzeit is not None else cur.strftime("%H:%M")
        try:
            ev["datetime"] = iso(parse_dt(dstr, tstr))
            ev["sent"] = []
        except Exception:
            return await interaction.followup.send("❌ Neues Datum/Uhrzeit ungültig.", ephemeral=True)

    save(d)
    await interaction.followup.send(f"✅ Termin **{termin_id}** aktualisiert.", ephemeral=True)

# ===== Todos =====
@bot.tree.command(name="todo", description="Erstellt ein Todo (public/private/user/role)")
@app_commands.describe(
    titel="Kurzbeschreibung", beschreibung="Optional", privat="true=privat",
    user="Optional: Person", rolle="Optional: Rolle",
    faellig_datum="Optional DD.MM.YYYY", faellig_uhrzeit="Optional HH:MM"
)
async def todo_add_cmd(
    interaction: discord.Interaction, titel: str, beschreibung: Optional[str]=None, privat: bool=False,
    user: Optional[discord.Member]=None, rolle: Optional[discord.Role]=None,
    faellig_datum: Optional[str]=None, faellig_uhrzeit: Optional[str]=None
):
    await interaction.response.defer(ephemeral=True)
    if user and rolle:
        return await interaction.followup.send("❌ Bitte entweder **user** oder **rolle** setzen (nicht beides).", ephemeral=True)

    scope, au, ar = ("private" if privat else "public"), None, None
    if not privat and user: scope, au = "user", user.id
    if not privat and rolle: scope, ar = "role", rolle.id

    due = None
    if faellig_datum:
        try:
            due = iso(parse_dt(faellig_datum, faellig_uhrzeit or "23:59"))
        except Exception:
            return await interaction.followup.send("❌ Fälligkeit ungültig. Beispiel: 10.03.2026 & 18:30", ephemeral=True)

    d = load()
    tid = next_id(d, "next_todo_id")
    d["todos"].append({
        "id": tid, "title": titel.strip(), "description": (beschreibung or "").strip(),
        "scope": scope, "assigned_user_id": au, "assigned_role_id": ar,
        "created_by": interaction.user.id, "created_at": iso(now()),
        "due": due, "done": False, "done_at": None, "deleted": False
    })
    save(d)

    tag = "🌍 öffentlich"
    if scope == "private": tag = "🔒 privat"
    elif scope == "user": tag = f"👤 für {user.display_name}"
    elif scope == "role": tag = f"👥 für @{rolle.name}"

    await interaction.followup.send(f"✅ Todo erstellt: **{tid}** · **{titel.strip()}** ({tag}){fmt_due(due)}", ephemeral=True)

@bot.tree.command(name="todos", description="Zeigt offene, relevante Todos")
async def todos_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    d = load()
    items = [t for t in d["todos"] if not t.get("deleted") and not t.get("done") and todo_relevant(t, member)]
    items.sort(key=lambda t: (from_iso(t["due"]) if t.get("due") else datetime.max.replace(tzinfo=TZ),
                              from_iso(t.get("created_at")) if t.get("created_at") else now()))
    if not items:
        return await interaction.followup.send("📭 Keine offenen Todos.", ephemeral=True)

    def tag(t):
        sc = t.get("scope", "public")
        return "öffentlich" if sc == "public" else ("privat" if sc == "private" else ("user" if sc == "user" else "rolle"))

    lines = []
    for t in items[:40]:
        desc = (t.get("description") or "")
        if desc: desc = " — " + desc[:60] + ("…" if len(desc) > 60 else "")
        lines.append(f"⬜ **{t['id']}** · **{t['title']}** ({tag(t)}){fmt_due(t.get('due'))}{desc}")
    if len(items) > 40: lines.append(f"… und {len(items)-40} weitere.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="oldtodos", description="Zeigt erledigte, relevante Todos")
async def oldtodos_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    d = load()
    items = [t for t in d["todos"] if not t.get("deleted") and t.get("done") and todo_relevant(t, member)]
    items.sort(key=lambda t: from_iso(t["done_at"]) if t.get("done_at") else datetime.min.replace(tzinfo=TZ), reverse=True)
    if not items:
        return await interaction.followup.send("📭 Keine erledigten Todos.", ephemeral=True)

    lines = []
    for t in items[:40]:
        done_txt = ""
        if t.get("done_at"):
            done_txt = " · erledigt: " + from_iso(t["done_at"]).strftime("%d.%m.%Y %H:%M")
        lines.append(f"✅ **{t['id']}** · **{t['title']}**{done_txt}")
    if len(items) > 40: lines.append(f"… und {len(items)-40} weitere.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="todo_done", description="Hakt ein Todo ab (per ID)")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_done_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_edit(t, member): return await interaction.followup.send("❌ Du darfst dieses Todo nicht abhaken.", ephemeral=True)
    if t.get("done"): return await interaction.followup.send("ℹ️ Dieses Todo ist bereits erledigt.", ephemeral=True)

    t["done"], t["done_at"] = True, iso(now())
    save(d)
    await interaction.followup.send(f"✅ Todo **{todo_id}** abgehakt.", ephemeral=True)

@bot.tree.command(name="todo_undo", description="Setzt ein Todo wieder auf offen (per ID)")
@app_commands.describe(todo_id="ID aus /oldtodos")
async def todo_undo_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_edit(t, member): return await interaction.followup.send("❌ Du darfst dieses Todo nicht ändern.", ephemeral=True)

    t["done"], t["done_at"] = False, None
    save(d)
    await interaction.followup.send(f"↩️ Todo **{todo_id}** wieder offen.", ephemeral=True)

@bot.tree.command(name="todo_delete", description="Löscht ein Todo (per ID)")
@app_commands.describe(todo_id="ID aus /todos oder /oldtodos")
async def todo_delete_cmd(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user

    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_edit(t, member): return await interaction.followup.send("❌ Du darfst dieses Todo nicht löschen.", ephemeral=True)

    t["deleted"] = True
    save(d)
    await interaction.followup.send(f"🗑️ Todo **{todo_id}** gelöscht.", ephemeral=True)

@bot.tree.command(name="todo_edit", description="Bearbeitet ein bestehendes Todo")
@app_commands.describe(todo_id="ID", titel="Optional", beschreibung="Optional", privat="Optional",
                      user="Optional", rolle="Optional", faellig_datum="Optional (leer=entfernen)", faellig_uhrzeit="Optional")
async def todo_edit_cmd(
    interaction: discord.Interaction, todo_id: int,
    titel: Optional[str]=None, beschreibung: Optional[str]=None, privat: Optional[bool]=None,
    user: Optional[discord.Member]=None, rolle: Optional[discord.Role]=None,
    faellig_datum: Optional[str]=None, faellig_uhrzeit: Optional[str]=None
):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    member: discord.Member = interaction.user
    if user and rolle:
        return await interaction.followup.send("❌ Bitte entweder **user** oder **rolle** setzen (nicht beides).", ephemeral=True)

    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_edit(t, member): return await interaction.followup.send("❌ Du darfst dieses Todo nicht bearbeiten.", ephemeral=True)

    if titel and titel.strip(): t["title"] = titel.strip()
    if beschreibung is not None: t["description"] = beschreibung.strip()

    if privat is True:
        t["scope"], t["assigned_user_id"], t["assigned_role_id"] = "private", None, None
    elif privat is False and user is None and rolle is None and t.get("scope") == "private":
        t["scope"] = "public"

    if user is not None:
        t["scope"], t["assigned_user_id"], t["assigned_role_id"] = "user", user.id, None
    if rolle is not None:
        t["scope"], t["assigned_role_id"], t["assigned_user_id"] = "role", rolle.id, None

    if faellig_datum is not None:
        if faellig_datum.strip() == "":
            t["due"] = None
        else:
            try:
                t["due"] = iso(parse_dt(faellig_datum, faellig_uhrzeit or "23:59"))
            except Exception:
                return await interaction.followup.send("❌ Fälligkeit ungültig. Beispiel: 15.03.2026 & 18:00", ephemeral=True)

    save(d)
    await interaction.followup.send(f"✅ Todo **{todo_id}** wurde aktualisiert.", ephemeral=True)

# ===== START =====
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
