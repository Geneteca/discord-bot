import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List

import discord
from discord import app_commands

# =========================
# ENV / KONFIG
# =========================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
TZ = ZoneInfo("Europe/Berlin")

# WICHTIG: MUSS gesetzt sein!
GUILD_ID = int(os.environ["GUILD_ID"])

DATA_FILE = "data.json"
CHECK_INTERVAL_SECONDS = 20

# =========================
# HELFER
# =========================
def now_berlin():
    return datetime.now(tz=TZ)

def parse_dt_berlin(d, t):
    return datetime.strptime(f"{d} {t}", "%d-%m-%Y %H:%M").replace(tzinfo=TZ)

def to_iso(dt):
    return dt.astimezone(TZ).isoformat()

def from_iso(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)

def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"events": [], "next_id": 1}

def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

def next_id(data):
    i = data["next_id"]
    data["next_id"] += 1
    return i

def normalize_reminders(s: str) -> List[int]:
    if not s:
        return []
    return sorted(set(int(x.strip()) for x in s.split(",") if x.strip()))

# =========================
# DISCORD CLIENT
# =========================
class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        # 👉 TEMPORÄR: GLOBALE COMMANDS LÖSCHEN
        print("🧹 Entferne globale Slash-Commands …", flush=True)
        self.tree.clear_commands(guild=None)
        await self.tree.sync()
        print("✅ Globale Slash-Commands gelöscht", flush=True)

        # Guild Commands setzen
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print(f"✅ Guild Slash-Commands synced ({GUILD_ID})", flush=True)

bot = Bot()

# =========================
# REMINDER LOOP
# =========================
async def reminder_loop():
    await bot.wait_until_ready()
    channel = bot.get_channel(ERINNERUNGS_CHANNEL_ID)

    print("⏰ Reminder Loop aktiv", flush=True)

    while True:
        data = load_data()
        now = now_berlin()
        changed = False

        for e in data["events"]:
            if e.get("cancelled"):
                continue

            dt = from_iso(e["datetime"])
            e.setdefault("sent", [])

            for m in e["reminders"]:
                if m not in e["sent"] and now >= dt - timedelta(minutes=m):
                    text = (
                        f"🔔 **Erinnerung** ({m} min vorher)\n"
                        f"📌 **{e['title']}**\n"
                        f"🕒 {dt.strftime('%d.%m.%Y %H:%M')} (Berlin)"
                    )

                    if e["target"]["type"] == "channel":
                        await channel.send(text)
                    else:
                        for uid in e["target"]["users"]:
                            user = await bot.fetch_user(uid)
                            await user.send(text)

                    e["sent"].append(m)
                    changed = True

            if now >= dt:
                if e["repeat"] != "none":
                    if e["repeat"] == "daily":
                        e["datetime"] = to_iso(dt + timedelta(days=1))
                    elif e["repeat"] == "weekly":
                        e["datetime"] = to_iso(dt + timedelta(weeks=1))
                    elif e["repeat"] == "monthly":
                        e["datetime"] = to_iso(dt + timedelta(days=30))
                    e["sent"] = []
                    changed = True
                else:
                    e["cancelled"] = True
                    changed = True

        if changed:
            save_data(data)

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="termin", description="Öffentlicher Termin")
async def termin(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerungen: str = "30",
    wiederholung: str = "none"
):
    await interaction.response.defer(ephemeral=True)

    dt = parse_dt_berlin(datum, uhrzeit)
    data = load_data()

    e = {
        "id": next_id(data),
        "title": titel,
        "datetime": to_iso(dt),
        "reminders": normalize_reminders(erinnerungen),
        "sent": [],
        "repeat": wiederholung,
        "cancelled": False,
        "target": {
            "type": "channel"
        }
    }

    data["events"].append(e)
    save_data(data)

    await interaction.followup.send("✅ Öffentlicher Termin gespeichert", ephemeral=True)

@bot.tree.command(name="ptermin", description="Privater Termin per DM")
async def ptermin(
    interaction: discord.Interaction,
    datum: str,
    uhrzeit: str,
    titel: str,
    erinnerungen: str = "30",
    person1: Optional[discord.Member] = None,
    person2: Optional[discord.Member] = None,
):
    await interaction.response.defer(ephemeral=True)

    users = {interaction.user.id}
    for p in (person1, person2):
        if p:
            users.add(p.id)

    dt = parse_dt_berlin(datum, uhrzeit)
    data = load_data()

    e = {
        "id": next_id(data),
        "title": titel,
        "datetime": to_iso(dt),
        "reminders": normalize_reminders(erinnerungen),
        "sent": [],
        "repeat": "none",
        "cancelled": False,
        "target": {
            "type": "dm",
            "users": list(users)
        }
    }

    data["events"].append(e)
    save_data(data)

    await interaction.followup.send("📩 Privater Termin gespeichert (DM)", ephemeral=True)

# =========================
# START
# =========================
async def main():
    async with bot:
        bot.loop.create_task(reminder_loop())
        await bot.start(BOT_TOKEN)

asyncio.run(main())
