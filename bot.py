import os
import json
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands
from discord import app_commands

# =====================================================
# KONFIG
# =====================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
ERINNERUNGS_CHANNEL_ID = int(os.environ["ERINNERUNGS_CHANNEL_ID"])
ROLLE_ID = int(os.environ["ROLLE_ID"])
GUILD_ID = int(os.environ["GUILD_ID"])

TZ = ZoneInfo("Europe/Berlin")
DATA_FILE = "data.json"
CHECK_INTERVAL = 20

# =====================================================
# HELPER
# =====================================================
def now():
    return datetime.now(tz=TZ)

def load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except:
        d = {}
    d.setdefault("todos", [])
    d.setdefault("next_todo_id", 1)
    return d

def save(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)

# =====================================================
# BOT
# =====================================================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =====================================================
# SYNC
# =====================================================
@bot.event
async def setup_hook():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    print("Slash Commands:", [c.name for c in await bot.tree.fetch_commands(guild=guild)])

@bot.event
async def on_ready():
    print(f"✅ Bot online als {bot.user}")

# =====================================================
# /PING
# =====================================================
@bot.tree.command(name="ping", description="Testet ob der Bot online ist")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🏓 Pong! `{round(bot.latency * 1000)} ms`",
        ephemeral=True
    )

# =====================================================
# /HELP
# =====================================================
@bot.tree.command(name="help", description="Übersicht aller Commands")
async def help_cmd(interaction: discord.Interaction):
    text = (
        "**📅 Termine**\n"
        "/termin – Öffentlichen Termin erstellen\n"
        "/ptermin – Privaten Termin (DM)\n"
        "/termine – Aktive Termine\n\n"
        "**📝 Todos**\n"
        "/todo – Todo erstellen\n"
        "/todos – Offene Todos anzeigen\n"
        "/oldtodos – Erledigte Todos anzeigen\n"
        "/todo_done – Todo abhaken\n"
        "/todo_edit – Todo bearbeiten\n\n"
        "**ℹ️ System**\n"
        "/ping – Bot-Status prüfen\n"
        "/help – Diese Hilfe"
    )
    await interaction.response.send_message(text, ephemeral=True)

# =====================================================
# TODO-RECHTE
# =====================================================
def relevant(todo, member):
    if todo.get("deleted"):
        return False
    if todo["scope"] == "public":
        return True
    if todo["scope"] == "private":
        return todo["created_by"] == member.id
    if todo["scope"] == "user":
        return todo.get("assigned_user") == member.id
    if todo["scope"] == "role":
        return any(r.id == todo.get("assigned_role") for r in member.roles)
    return False

def can_edit(todo, member):
    return todo["created_by"] == member.id or member.guild_permissions.manage_guild

# =====================================================
# /TODO ADD
# =====================================================
@bot.tree.command(name="todo", description="Todo erstellen")
async def todo(
    interaction: discord.Interaction,
    titel: str,
    privat: bool = False,
    user: Optional[discord.Member] = None,
    rolle: Optional[discord.Role] = None
):
    d = load()
    tid = d["next_todo_id"]
    d["next_todo_id"] += 1

    scope = "private" if privat else "public"
    if user:
        scope = "user"
    if rolle:
        scope = "role"

    d["todos"].append({
        "id": tid,
        "title": titel,
        "scope": scope,
        "assigned_user": user.id if user else None,
        "assigned_role": rolle.id if rolle else None,
        "created_by": interaction.user.id,
        "done": False
    })
    save(d)

    await interaction.response.send_message(f"✅ Todo **{tid}** erstellt", ephemeral=True)

# =====================================================
# /TODOS (NUR OFFENE)
# =====================================================
@bot.tree.command(name="todos", description="Offene Todos anzeigen")
async def todos(interaction: discord.Interaction):
    d = load()
    member = interaction.user

    lines = [
        f"⬜ **{t['id']}** · {t['title']}"
        for t in d["todos"]
        if not t["done"] and relevant(t, member)
    ]

    await interaction.response.send_message(
        "\n".join(lines) if lines else "📭 Keine offenen Todos",
        ephemeral=True
    )

# =====================================================
# /OLDTODOS (ERLEDIGTE)
# =====================================================
@bot.tree.command(name="oldtodos", description="Erledigte Todos anzeigen")
async def oldtodos(interaction: discord.Interaction):
    d = load()
    member = interaction.user

    lines = [
        f"✅ **{t['id']}** · {t['title']}"
        for t in d["todos"]
        if t["done"] and relevant(t, member)
    ]

    await interaction.response.send_message(
        "\n".join(lines) if lines else "📭 Keine erledigten Todos",
        ephemeral=True
    )

# =====================================================
# /TODO DONE
# =====================================================
@bot.tree.command(name="todo_done", description="Todo abhaken")
async def todo_done(interaction: discord.Interaction, todo_id: int):
    d = load()
    member = interaction.user

    for t in d["todos"]:
        if t["id"] == todo_id and can_edit(t, member):
            t["done"] = True
            save(d)
            await interaction.response.send_message(f"✅ Todo **{todo_id}** erledigt", ephemeral=True)
            return

    await interaction.response.send_message("❌ Kein Zugriff oder Todo nicht gefunden", ephemeral=True)

# =====================================================
# /TODO EDIT
# =====================================================
@bot.tree.command(name="todo_edit", description="Todo bearbeiten")
async def todo_edit(
    interaction: discord.Interaction,
    todo_id: int,
    titel: Optional[str] = None,
    privat: Optional[bool] = None,
    user: Optional[discord.Member] = None,
    rolle: Optional[discord.Role] = None
):
    d = load()
    member = interaction.user

    for t in d["todos"]:
        if t["id"] != todo_id or not can_edit(t, member):
            continue

        if titel:
            t["title"] = titel

        if privat is True:
            t["scope"] = "private"
            t["assigned_user"] = None
            t["assigned_role"] = None
        elif user:
            t["scope"] = "user"
            t["assigned_user"] = user.id
        elif rolle:
            t["scope"] = "role"
            t["assigned_role"] = rolle.id
        elif privat is False:
            t["scope"] = "public"

        save(d)
        await interaction.response.send_message(f"✏️ Todo **{todo_id}** aktualisiert", ephemeral=True)
        return

    await interaction.response.send_message("❌ Todo nicht gefunden / keine Rechte", ephemeral=True)

# =====================================================
# START
# =====================================================
bot.run(BOT_TOKEN)
