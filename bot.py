import os, json, asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any, Set, Tuple

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
AUTO_DELETE_SECONDS = 900
CHECK_INTERVAL_SECONDS = 20
PAGE_SIZE = 6

REC_CHOICES = [app_commands.Choice(name=x, value=x) for x in ("none","daily","weekly","monthly")]

# ===== DATA =====
def load() -> Dict[str, Any]:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f: d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("events", []); d.setdefault("next_event_id", 1)
    d.setdefault("todos", []); d.setdefault("next_todo_id", 1)
    return d

def save(d: Dict[str, Any]): 
    with open(DATA_FILE, "w", encoding="utf-8") as f: json.dump(d, f, indent=2, ensure_ascii=False)

def next_id(d: Dict[str, Any], key: str) -> int:
    nid = int(d.get(key, 1)); d[key] = nid + 1; return nid

# ===== TIME =====
def now() -> datetime: return datetime.now(tz=TZ)

def to_iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(TZ).isoformat()

def from_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=TZ)).astimezone(TZ)

def parse_dt(d: str, t: str) -> datetime:
    return datetime.strptime(f"{d} {t}", "%d.%m.%Y %H:%M").replace(tzinfo=TZ)

def add_month(dt: datetime) -> datetime:
    y, m = dt.year, dt.month + 1
    if m == 13: y, m = y + 1, 1
    next_m = datetime(y + (m==12), 1 if m==12 else m+1, 1, tzinfo=dt.tzinfo)
    last = (next_m - timedelta(days=1)).day
    return dt.replace(year=y, month=m, day=min(dt.day, last))

def next_occ(dt: datetime, rec: str) -> datetime:
    return dt + timedelta(days=1) if rec=="daily" else dt + timedelta(weeks=1) if rec=="weekly" else add_month(dt) if rec=="monthly" else dt

def parse_reminders(s: str) -> List[int]:
    s = (s or "").strip()
    if not s: return []
    out=[]
    for p in [x.strip().lower() for x in s.split(",") if x.strip()]:
        if p.endswith("m"): out.append(int(p[:-1]))
        elif p.endswith("h"): out.append(int(p[:-1]) * 60)
        elif p.endswith("d"): out.append(int(p[:-1]) * 1440)
        else: out.append(int(p))
    return sorted(set(x for x in out if x >= 0), reverse=True)

def fmt_due(due_iso: Optional[str]) -> str:
    if not due_iso: return ""
    try: return " · fällig: " + from_iso(due_iso).strftime("%d.%m.%Y %H:%M")
    except: return ""

def reminder_msg(title: str, dt: datetime, m: int) -> str:
    return f"🔔 **Erinnerung** ({m} min vorher)\n📌 **{title}**\n🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)"

# ===== BOT =====
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def ch_send(cid: int, content: str):
    ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
    await ch.send(content, delete_after=AUTO_DELETE_SECONDS)

async def dm_send(uid: int, content: str):
    u = bot.get_user(uid) or await bot.fetch_user(uid)
    await u.send(content)

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}", flush=True)

@bot.event
async def setup_hook():
    await sync_cmds()
    bot.loop.create_task(reminder_loop())

async def sync_cmds():
    g = discord.Object(id=GUILD_ID)
    if CLEAN_GLOBAL_COMMANDS:
        print("🧹 Lösche globale Slash-Commands …", flush=True)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("✅ Globale Slash-Commands gelöscht.", flush=True)
    bot.tree.copy_global_to(guild=g)
    await bot.tree.sync(guild=g)
    remote = await bot.tree.fetch_commands(guild=g)
    print(f"✅ Slash-Commands synced to guild {GUILD_ID}", flush=True)
    print(f"📌 Remote Commands (Guild): {[c.name for c in remote]}", flush=True)

# ===== REMINDER LOOP =====
async def reminder_loop():
    await bot.wait_until_ready()
    print("⏰ Reminder-Loop aktiv", flush=True)
    while not bot.is_closed():
        try:
            d = load(); changed=False; n = now()
            for e in d["events"]:
                if e.get("cancelled"): 
                    continue
                dt = from_iso(e["datetime"])
                rems = [int(x) for x in e.get("reminders", [])]
                sent = set(int(x) for x in e.get("sent", []))

                for m in rems:
                    if m in sent: 
                        continue
                    if n >= dt - timedelta(minutes=m) and n < dt + timedelta(hours=24):
                        msg = reminder_msg(e["title"], dt, m)
                        tgt = e["target"]
                        if tgt["type"] == "channel":
                            await ch_send(tgt["channel_id"], f"<@&{ROLLE_ID}> {msg}")
                        else:
                            for uid in tgt["user_ids"]:
                                await dm_send(uid, msg)
                        sent.add(m); e["sent"] = sorted(sent, reverse=True); changed=True

                if n >= dt:
                    rec = (e.get("recurrence") or "none").lower()
                    if rec != "none":
                        e["datetime"] = to_iso(next_occ(dt, rec))
                        e["sent"] = []
                    else:
                        e["cancelled"] = True
                    changed=True

            if changed: save(d)
        except Exception as ex:
            print(f"❌ Reminder-Loop Fehler: {type(ex).__name__}: {ex}", flush=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ===== TODO PERMS =====
def role_ids(m: discord.Member) -> Set[int]:
    return {r.id for r in getattr(m, "roles", [])}

def todo_relevant(t: Dict[str, Any], m: discord.Member) -> bool:
    if t.get("deleted"): return False
    sc = t.get("scope", "public")
    if sc=="public": return True
    if sc=="private": return int(t.get("created_by",0)) == m.id
    if sc=="user": return int(t.get("assigned_user_id",0)) == m.id or int(t.get("created_by",0)) == m.id
    if sc=="role": return int(t.get("assigned_role_id",0)) in role_ids(m) or int(t.get("created_by",0)) == m.id
    return False

def todo_can_modify(t: Dict[str, Any], m: discord.Member) -> bool:
    if int(t.get("created_by",0)) == m.id: return True
    if t.get("scope")=="user" and int(t.get("assigned_user_id",0)) == m.id: return True
    return m.guild_permissions.manage_guild

# ===== BASIC =====
@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! `{round(bot.latency*1000)} ms`", ephemeral=True)

@bot.tree.command(name="help", description="Übersicht aller Commands")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**ℹ️ Allgemein**\n/ping\n/help\n/dashboard\n\n"
        "**📅 Termine**\n/termin /ptermin\n/termine /termine_all\n/termin_edit /termin_absagen\n\n"
        "**📝 Todos**\n/todo\n/todos /oldtodos\n/todo_done /todo_undo\n/todo_edit /todo_delete\n",
        ephemeral=True
    )

# ===== EVENTS =====
@bot.tree.command(name="termin", description="Öffentlicher Termin (Channel) mit Rollen-Ping")
@app_commands.describe(datum="DD.MM.YYYY", uhrzeit="HH:MM", titel="Titel", erinnerung="z.B. 60,10,5", wiederholung="none/daily/weekly/monthly")
@app_commands.choices(wiederholung=REC_CHOICES)
async def termin(interaction: discord.Interaction, datum: str, uhrzeit: str, titel: str, erinnerung: str="30", wiederholung: str="none"):
    await interaction.response.defer(ephemeral=True)
    try: dt = parse_dt(datum, uhrzeit)
    except: return await interaction.followup.send("❌ Ungültig. Beispiel: 08.02.2026 & 12:00", ephemeral=True)

    d = load(); eid = next_id(d, "next_event_id"); rems = parse_reminders(erinnerung)
    d["events"].append({
        "id": eid, "title": titel.strip(), "datetime": to_iso(dt),
        "reminders": rems, "sent": [], "recurrence": wiederholung,
        "cancelled": False, "target": {"type":"channel","channel_id":ERINNERUNGS_CHANNEL_ID},
        "created_by": interaction.user.id
    })
    save(d)
    rem_txt = ", ".join(f"{m}m" for m in rems) if rems else "—"
    await ch_send(ERINNERUNGS_CHANNEL_ID,
        f"<@&{ROLLE_ID}> 📅 **Neuer Termin**\n📌 **{titel}**\n🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)\n🔔 **Erinnerung:** {rem_txt} vorher\n🆔 ID: **{eid}**"
    )
    await interaction.followup.send(f"✅ Termin gespeichert. ID: **{eid}**", ephemeral=True)

@bot.tree.command(name="ptermin", description="Privater Termin per DM (ohne Rollen-Ping in DM)")
@app_commands.describe(datum="DD.MM.YYYY", uhrzeit="HH:MM", titel="Titel", erinnerung="z.B. 60,10,5", wiederholung="none/daily/weekly/monthly",
                      person1="Optional", person2="Optional", person3="Optional", person4="Optional", person5="Optional")
@app_commands.choices(wiederholung=REC_CHOICES)
async def ptermin(interaction: discord.Interaction, datum: str, uhrzeit: str, titel: str, erinnerung: str="30", wiederholung: str="none",
                  person1: Optional[discord.Member]=None, person2: Optional[discord.Member]=None, person3: Optional[discord.Member]=None,
                  person4: Optional[discord.Member]=None, person5: Optional[discord.Member]=None):
    await interaction.response.defer(ephemeral=True)
    try: dt = parse_dt(datum, uhrzeit)
    except: return await interaction.followup.send("❌ Ungültig. Beispiel: 08.02.2026 & 12:00", ephemeral=True)

    ids = {interaction.user.id} | {p.id for p in (person1,person2,person3,person4,person5) if p}
    d = load(); eid = next_id(d, "next_event_id")
    d["events"].append({
        "id": eid, "title": titel.strip(), "datetime": to_iso(dt),
        "reminders": parse_reminders(erinnerung), "sent": [],
        "recurrence": wiederholung, "cancelled": False,
        "target": {"type":"dm","user_ids": sorted(ids)},
        "created_by": interaction.user.id
    })
    save(d)
    await interaction.followup.send(f"✅ Privater Termin gespeichert. ID: **{eid}**. Empfänger: **{len(ids)}**", ephemeral=True)

@bot.tree.command(name="termine", description="Zeigt nur aktive (zukünftige) Termine")
async def termine(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    d = load(); n = now()
    evs = [e for e in d["events"] if not e.get("cancelled") and from_iso(e["datetime"]) >= n]
    evs.sort(key=lambda e: from_iso(e["datetime"]))
    if not evs: return await interaction.followup.send("📭 Keine aktiven Termine.", ephemeral=True)
    lines=[]
    for e in evs[:25]:
        dt = from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termine_all", description="Zeigt alle Termine (inkl. alte/abgesagte)")
async def termine_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    d = load()
    evs = sorted(d["events"], key=lambda e: from_iso(e["datetime"]))
    if not evs: return await interaction.followup.send("📭 Keine Termine gespeichert.", ephemeral=True)
    lines=[]
    for e in evs[:25]:
        dt = from_iso(e["datetime"])
        rems = ",".join(str(m) for m in e.get("reminders", [])) or "—"
        status = "abgesagt/erledigt" if e.get("cancelled") else "aktiv"
        lines.append(f"**{e['id']}** · {dt.strftime('%d.%m.%Y %H:%M')} · **{e['title']}** · rem: {rems} · {e.get('recurrence','none')} · {e['target']['type']} · {status}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="termin_absagen", description="Sagt einen Termin ab (per ID)")
@app_commands.describe(termin_id="ID aus /termine oder /termine_all")
async def termin_absagen(interaction: discord.Interaction, termin_id: int):
    await interaction.response.defer(ephemeral=True)
    d = load()
    for e in d["events"]:
        if int(e.get("id",-1)) == int(termin_id) and not e.get("cancelled"):
            e["cancelled"]=True; save(d)
            return await interaction.followup.send(f"❌ Termin **{termin_id}** abgesagt.", ephemeral=True)
    await interaction.followup.send("❌ Termin-ID nicht gefunden oder schon abgesagt.", ephemeral=True)

@bot.tree.command(name="termin_edit", description="Bearbeitet einen Termin (per ID)")
@app_commands.describe(termin_id="ID", datum="Optional DD.MM.YYYY", uhrzeit="Optional HH:MM", titel="Optional", erinnerung="Optional z.B. 120,30,10", wiederholung="Optional")
@app_commands.choices(wiederholung=REC_CHOICES)
async def termin_edit(interaction: discord.Interaction, termin_id: int, datum: Optional[str]=None, uhrzeit: Optional[str]=None,
                      titel: Optional[str]=None, erinnerung: Optional[str]=None, wiederholung: Optional[str]=None):
    await interaction.response.defer(ephemeral=True)
    d = load()
    ev = next((e for e in d["events"] if int(e.get("id",-1)) == int(termin_id) and not e.get("cancelled")), None)
    if not ev: return await interaction.followup.send("❌ Termin-ID nicht gefunden.", ephemeral=True)

    if titel and titel.strip(): ev["title"] = titel.strip()
    if erinnerung is not None: ev["reminders"] = parse_reminders(erinnerung); ev["sent"] = []
    if wiederholung is not None: ev["recurrence"] = wiederholung

    if datum is not None or uhrzeit is not None:
        cur = from_iso(ev["datetime"])
        dstr = datum if datum is not None else cur.strftime("%d.%m.%Y")
        tstr = uhrzeit if uhrzeit is not None else cur.strftime("%H:%M")
        try:
            ev["datetime"] = to_iso(parse_dt(dstr, tstr)); ev["sent"]=[]
        except:
            return await interaction.followup.send("❌ Neues Datum/Uhrzeit ungültig.", ephemeral=True)

    save(d)
    await interaction.followup.send(f"✅ Termin **{termin_id}** aktualisiert.", ephemeral=True)

# ===== TODOS =====
@bot.tree.command(name="todo", description="Erstellt ein Todo (public/private/user/role)")
@app_commands.describe(titel="Kurzbeschreibung", beschreibung="Optional", privat="true=privat", user="Optional", rolle="Optional",
                      faellig_datum="Optional DD.MM.YYYY", faellig_uhrzeit="Optional HH:MM")
async def todo(interaction: discord.Interaction, titel: str, beschreibung: Optional[str]=None, privat: bool=False,
               user: Optional[discord.Member]=None, rolle: Optional[discord.Role]=None,
               faellig_datum: Optional[str]=None, faellig_uhrzeit: Optional[str]=None):
    await interaction.response.defer(ephemeral=True)
    if user and rolle: return await interaction.followup.send("❌ Bitte entweder user oder rolle (nicht beides).", ephemeral=True)

    scope = "private" if privat else "public"
    au=ar=None
    if not privat and user: scope, au = "user", user.id
    if not privat and rolle: scope, ar = "role", rolle.id

    due=None
    if faellig_datum:
        try: due = to_iso(parse_dt(faellig_datum, faellig_uhrzeit or "23:59"))
        except: return await interaction.followup.send("❌ Fälligkeit ungültig. Beispiel: 10.03.2026 & 18:30", ephemeral=True)

    d = load(); tid = next_id(d, "next_todo_id")
    d["todos"].append({
        "id": tid, "title": titel.strip(), "description": (beschreibung or "").strip(),
        "scope": scope, "assigned_user_id": au, "assigned_role_id": ar,
        "created_by": interaction.user.id, "created_at": to_iso(now()),
        "due": due, "done": False, "done_at": None, "deleted": False
    })
    save(d)
    await interaction.followup.send(f"✅ Todo erstellt: **{tid}** · **{titel.strip()}**{fmt_due(due)}", ephemeral=True)

@bot.tree.command(name="todos", description="Zeigt offene, relevante Todos")
async def todos(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    m: discord.Member = interaction.user
    d = load()
    items = [t for t in d["todos"] if not t.get("deleted") and not t.get("done") and todo_relevant(t, m)]
    if not items: return await interaction.followup.send("📭 Keine offenen Todos.", ephemeral=True)

    def key(t):
        due = t.get("due"); due_dt = from_iso(due) if due else datetime.max.replace(tzinfo=TZ)
        created = from_iso(t.get("created_at")) if t.get("created_at") else now()
        return (due_dt, created)
    items.sort(key=key)

    lines=[]
    for t in items[:40]:
        desc = (t.get("description") or "")
        if desc: desc = " — " + desc[:60] + ("…" if len(desc)>60 else "")
        lines.append(f"⬜ **{t['id']}** · **{t['title']}**{fmt_due(t.get('due'))}{desc}")
    if len(items)>40: lines.append(f"… und {len(items)-40} weitere.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="oldtodos", description="Zeigt erledigte, relevante Todos")
async def oldtodos(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    m: discord.Member = interaction.user
    d = load()
    items = [t for t in d["todos"] if not t.get("deleted") and t.get("done") and todo_relevant(t, m)]
    if not items: return await interaction.followup.send("📭 Keine erledigten Todos.", ephemeral=True)
    items.sort(key=lambda t: from_iso(t["done_at"]) if t.get("done_at") else datetime.min.replace(tzinfo=TZ), reverse=True)

    lines=[]
    for t in items[:40]:
        done_txt = ""
        if t.get("done_at"):
            done_txt = " · erledigt: " + from_iso(t["done_at"]).strftime("%d.%m.%Y %H:%M")
        lines.append(f"✅ **{t['id']}** · **{t['title']}**{done_txt}")
    if len(items)>40: lines.append(f"… und {len(items)-40} weitere.")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

async def _todo_set_done(interaction: discord.Interaction, todo_id: int, done: bool):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    m: discord.Member = interaction.user
    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_modify(t, m): return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)
    t["done"] = done
    t["done_at"] = to_iso(now()) if done else None
    save(d)
    await interaction.followup.send(("✅" if done else "↩️") + f" Todo **{todo_id}** {'abgehakt' if done else 'wieder offen'}.", ephemeral=True)

@bot.tree.command(name="todo_done", description="Hakt ein Todo ab (per ID)")
@app_commands.describe(todo_id="ID aus /todos")
async def todo_done(interaction: discord.Interaction, todo_id: int):
    await _todo_set_done(interaction, todo_id, True)

@bot.tree.command(name="todo_undo", description="Setzt ein Todo wieder auf offen (per ID)")
@app_commands.describe(todo_id="ID aus /oldtodos")
async def todo_undo(interaction: discord.Interaction, todo_id: int):
    await _todo_set_done(interaction, todo_id, False)

@bot.tree.command(name="todo_delete", description="Löscht ein Todo (per ID)")
@app_commands.describe(todo_id="ID")
async def todo_delete(interaction: discord.Interaction, todo_id: int):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    m: discord.Member = interaction.user
    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_modify(t, m): return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)
    t["deleted"]=True; save(d)
    await interaction.followup.send(f"🗑️ Todo **{todo_id}** gelöscht.", ephemeral=True)

@bot.tree.command(name="todo_edit", description="Bearbeitet ein bestehendes Todo")
@app_commands.describe(todo_id="ID", titel="Optional", beschreibung="Optional", privat="Optional",
                      user="Optional", rolle="Optional", faellig_datum="Optional (leer=entfernen)", faellig_uhrzeit="Optional")
async def todo_edit(interaction: discord.Interaction, todo_id: int, titel: Optional[str]=None, beschreibung: Optional[str]=None,
                    privat: Optional[bool]=None, user: Optional[discord.Member]=None, rolle: Optional[discord.Role]=None,
                    faellig_datum: Optional[str]=None, faellig_uhrzeit: Optional[str]=None):
    await interaction.response.defer(ephemeral=True)
    if not isinstance(interaction.user, discord.Member):
        return await interaction.followup.send("❌ Bitte im Server ausführen.", ephemeral=True)
    m: discord.Member = interaction.user
    if user and rolle: return await interaction.followup.send("❌ Bitte entweder user oder rolle (nicht beides).", ephemeral=True)

    d = load()
    t = next((x for x in d["todos"] if int(x.get("id",-1)) == int(todo_id) and not x.get("deleted")), None)
    if not t: return await interaction.followup.send("❌ Todo-ID nicht gefunden.", ephemeral=True)
    if not todo_can_modify(t, m): return await interaction.followup.send("❌ Keine Rechte.", ephemeral=True)

    if titel and titel.strip(): t["title"] = titel.strip()
    if beschreibung is not None: t["description"] = beschreibung.strip()

    if privat is True:
        t["scope"]="private"; t["assigned_user_id"]=None; t["assigned_role_id"]=None
    elif privat is False and user is None and rolle is None and t.get("scope")=="private":
        t["scope"]="public"

    if user is not None:
        t["scope"]="user"; t["assigned_user_id"]=user.id; t["assigned_role_id"]=None
    if rolle is not None:
        t["scope"]="role"; t["assigned_role_id"]=rolle.id; t["assigned_user_id"]=None

    if faellig_datum is not None:
        if faellig_datum.strip()=="":
            t["due"]=None
        else:
            try: t["due"]=to_iso(parse_dt(faellig_datum, faellig_uhrzeit or "23:59"))
            except: return await interaction.followup.send("❌ Fälligkeit ungültig.", ephemeral=True)

    save(d)
    await interaction.followup.send(f"✅ Todo **{todo_id}** aktualisiert.", ephemeral=True)

# ===== DASHBOARD (ephemeral) =====
def dash_items(m: discord.Member, tab: str) -> List[Dict[str, Any]]:
    d = load(); n = now()
    if tab=="todos_open":
        items=[t for t in d["todos"] if not t.get("deleted") and not t.get("done") and todo_relevant(t,m)]
        def key(t):
            due=t.get("due"); due_dt=from_iso(due) if due else datetime.max.replace(tzinfo=TZ)
            created=from_iso(t.get("created_at")) if t.get("created_at") else n
            return (due_dt, created)
        items.sort(key=key); return items
    if tab=="todos_done":
        items=[t for t in d["todos"] if not t.get("deleted") and t.get("done") and todo_relevant(t,m)]
        items.sort(key=lambda t: from_iso(t["done_at"]) if t.get("done_at") else datetime.min.replace(tzinfo=TZ), reverse=True); return items
    if tab=="events_active":
        items=[e for e in d["events"] if not e.get("cancelled") and from_iso(e["datetime"]) >= n]
        items.sort(key=lambda e: from_iso(e["datetime"])); return items
    items=list(d["events"]); items.sort(key=lambda e: from_iso(e["datetime"])); return items

def dash_page(items: List[Dict[str, Any]], page: int) -> Tuple[List[Dict[str, Any]], int, int]:
    total=len(items); pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    page=max(0,min(page,pages-1))
    return items[page*PAGE_SIZE:page*PAGE_SIZE+PAGE_SIZE], page, pages

def dash_embed(m: discord.Member, tab: str, page: int, sel: Optional[int]) -> discord.Embed:
    items=dash_items(m, tab)
    sl, page, pages = dash_page(items, page)
    title={"todos_open":"📝 Todos – offen","todos_done":"✅ Todos – erledigt","events_active":"📅 Termine – aktiv","events_all":"📦 Termine – alle"}[tab]
    e=discord.Embed(title=f"🧠 Dashboard · {title}", color=0x5865F2)
    e.set_footer(text=f"Seite {page+1}/{pages} · Auswahl: {sel if sel else '—'}")
    if not sl: e.description="📭 Keine Einträge."; return e
    if tab.startswith("todos"):
        for t in sl:
            st="✅" if t.get("done") else "⬜"
            sc={"public":"öffentlich","private":"privat","user":"user","role":"rolle"}.get(t.get("scope","public"),t.get("scope","public"))
            desc=(t.get("description") or "—")
            e.add_field(name=f"{st} ID {t['id']} · {t.get('title','—')} ({sc}){fmt_due(t.get('due'))}",
                        value=desc[:180]+("…" if len(desc)>180 else ""), inline=False)
    else:
        for it in sl:
            dt=from_iso(it["datetime"]); st="❌" if it.get("cancelled") else "📅"
            rem=",".join(str(x) for x in it.get("reminders",[])) or "—"
            e.add_field(name=f"{st} ID {it['id']} · {it.get('title','—')}",
                        value=f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} · 🔔 {rem} · 🔁 {it.get('recurrence','none')} · 🎯 {it.get('target',{}).get('type','channel')}",
                        inline=False)
    return e

def dash_opts(m: discord.Member, tab: str, page: int) -> List[discord.SelectOption]:
    items=dash_items(m, tab); sl, _, _ = dash_page(items, page)
    if not sl: return [discord.SelectOption(label="Keine Einträge", value="0")]
    out=[]
    for it in sl:
        if tab.startswith("todos"):
            out.append(discord.SelectOption(label=f"{it['id']} · {it.get('title','—')[:60]}", description=f"todo {it.get('scope','public')}"[:100], value=str(it["id"])))
        else:
            dt=from_iso(it["datetime"]).strftime("%d.%m.%Y %H:%M")
            out.append(discord.SelectOption(label=f"{it['id']} · {it.get('title','—')[:50]}", description=dt, value=str(it["id"])))
    return out

class DashSelect(discord.ui.Select):
    def __init__(self, view: "DashView"):
        self.v=view
        super().__init__(placeholder="Eintrag auswählen…", options=dash_opts(view.member, view.tab, view.page), min_values=1, max_values=1)
    async def callback(self, interaction: discord.Interaction):
        if self.values and self.values[0]!="0": self.v.selected=int(self.values[0])
        await self.v.refresh(interaction)

class DashView(discord.ui.View):
    def __init__(self, member: discord.Member, tab="todos_open", page=0, selected: Optional[int]=None):
        super().__init__(timeout=600)
        self.member=member; self.owner=member.id; self.tab=tab; self.page=page; self.selected=selected
        self.add_item(DashSelect(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner:
            await interaction.response.send_message("❌ Nicht dein Dashboard.", ephemeral=True); return False
        return True

    def rebuild(self): return DashView(self.member, self.tab, self.page, self.selected)

    async def refresh(self, interaction: discord.Interaction):
        emb=dash_embed(self.member, self.tab, self.page, self.selected)
        await interaction.response.edit_message(embed=emb, view=self.rebuild())

    @discord.ui.button(label="📝", style=discord.ButtonStyle.primary, row=1)
    async def t1(self, interaction: discord.Interaction, _): self.tab="todos_open"; self.page=0; self.selected=None; await self.refresh(interaction)
    @discord.ui.button(label="✅", style=discord.ButtonStyle.secondary, row=1)
    async def t2(self, interaction: discord.Interaction, _): self.tab="todos_done"; self.page=0; self.selected=None; await self.refresh(interaction)
    @discord.ui.button(label="📅", style=discord.ButtonStyle.success, row=1)
    async def t3(self, interaction: discord.Interaction, _): self.tab="events_active"; self.page=0; self.selected=None; await self.refresh(interaction)
    @discord.ui.button(label="📦", style=discord.ButtonStyle.secondary, row=1)
    async def t4(self, interaction: discord.Interaction, _): self.tab="events_all"; self.page=0; self.selected=None; await self.refresh(interaction)

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary, row=2)
    async def prev(self, interaction: discord.Interaction, _): self.page=max(0,self.page-1); self.selected=None; await self.refresh(interaction)
    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary, row=2)
    async def nxt(self, interaction: discord.Interaction, _): self.page+=1; self.selected=None; await self.refresh(interaction)
    @discord.ui.button(label="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def ref(self, interaction: discord.Interaction, _): await self.refresh(interaction)

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=3)
    async def done(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        d=load(); t=next((x for x in d["todos"] if int(x.get("id",-1))==self.selected and not x.get("deleted")), None)
        if not t: return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not todo_can_modify(t, self.member): return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        t["done"]=True; t["done_at"]=to_iso(now()); save(d)
        await interaction.response.send_message(f"✅ Todo {self.selected} erledigt.", ephemeral=True)

    @discord.ui.button(label="↩️ Undo", style=discord.ButtonStyle.primary, row=3)
    async def undo(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        d=load(); t=next((x for x in d["todos"] if int(x.get("id",-1))==self.selected and not x.get("deleted")), None)
        if not t: return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not todo_can_modify(t, self.member): return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        t["done"]=False; t["done_at"]=None; save(d)
        await interaction.response.send_message(f"↩️ Todo {self.selected} wieder offen.", ephemeral=True)

    @discord.ui.button(label="🗑️", style=discord.ButtonStyle.danger, row=3)
    async def delete(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("todos") or not self.selected:
            return await interaction.response.send_message("❌ Erst ein Todo auswählen.", ephemeral=True)
        d=load(); t=next((x for x in d["todos"] if int(x.get("id",-1))==self.selected and not x.get("deleted")), None)
        if not t: return await interaction.response.send_message("❌ Todo nicht gefunden.", ephemeral=True)
        if not todo_can_modify(t, self.member): return await interaction.response.send_message("❌ Keine Rechte.", ephemeral=True)
        t["deleted"]=True; save(d)
        await interaction.response.send_message(f"🗑️ Todo {self.selected} gelöscht.", ephemeral=True)

    @discord.ui.button(label="❌ Termin", style=discord.ButtonStyle.danger, row=4)
    async def cancel_ev(self, interaction: discord.Interaction, _):
        if not self.tab.startswith("events") or not self.selected:
            return await interaction.response.send_message("❌ Erst Termin auswählen.", ephemeral=True)
        d=load(); ev=next((x for x in d["events"] if int(x.get("id",-1))==self.selected), None)
        if not ev: return await interaction.response.send_message("❌ Termin nicht gefunden.", ephemeral=True)
        ev["cancelled"]=True; save(d)
        await interaction.response.send_message(f"❌ Termin {self.selected} abgesagt.", ephemeral=True)

@bot.tree.command(name="dashboard", description="Interaktives Dashboard (Todos + Termine)")
async def dashboard(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Bitte im Server ausführen.", ephemeral=True)
    tab="todos_open"; page=0
    await interaction.response.send_message(embed=dash_embed(interaction.user, tab, page, None), view=DashView(interaction.user, tab, page, None), ephemeral=True)

# ===== START =====
if __name__ == "__main__":
    bot.run(BOT_TOKEN)
