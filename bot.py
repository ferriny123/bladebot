import asyncio
import json
import math
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord

CFG = "config.json"
DATA = "hive_data.json"
API = "https://discord.com/api/v10"


def load(path, default):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


config = load(CFG, {"token": "PASTE_YOUR_BOT_TOKEN_HERE"})
data = load(DATA, {
    "channel_id": None,
    "role_id": None,
    "end_time": None,
    "duration_hours": 10,
    "default_hours": 10,
    "warning_sent": False,
})
data.setdefault("default_hours", 10)
data.setdefault("duration_hours", data["default_hours"])
data.setdefault("warning_sent", False)


def save():
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def parse_hours(value):
    try:
        hours = float(value)
        return hours if 0.1 <= hours <= 168 else None
    except (TypeError, ValueError):
        return None


def get_option(options, name):
    for option in options or []:
        if option.get("name") == name:
            return option.get("value")
    return None


# These are registered directly with Discord's REST API. This deliberately
# avoids discord.py's app_commands transformer for /hivechannel, while still
# giving Discord a real CHANNEL (type 7) option and therefore its native picker.
COMMANDS = [
    {
        "name": "hives",
        "description": "Blade Wasp Hive timer",
        "type": 1,
        "options": [
            {
                "name": "start",
                "description": "Start the Hive timer",
                "type": 1,
                "options": [
                    {"name": "hours", "description": "Timer length in hours (default is saved default)", "type": 10, "required": False, "min_value": 0.1, "max_value": 168}
                ],
            },
            {
                "name": "reset",
                "description": "Reset the Hive timer",
                "type": 1,
                "options": [
                    {"name": "hours", "description": "New timer length (default is saved default)", "type": 10, "required": False, "min_value": 0.1, "max_value": 168}
                ],
            },
            {
                "name": "settime",
                "description": "Set the default Hive timer length",
                "type": 1,
                "options": [
                    {"name": "hours", "description": "New default timer length in hours", "type": 10, "required": True, "min_value": 0.1, "max_value": 168}
                ],
            },
            {"name": "stop", "description": "Stop the Hive timer", "type": 1},
            {"name": "status", "description": "Show the remaining Hive time", "type": 1},
        ],
    },
    {
        "name": "hiverole",
        "description": "Set the role that receives Hive reminders",
        "type": 1,
        "options": [{"name": "role", "description": "Role to ping", "type": 8, "required": True}],
    },
    {
        "name": "hivechannel",
        "description": "Set the channel for Hive reminders",
        "type": 1,
        "options": [{"name": "channel", "description": "Channel for Hive commands and reminders", "type": 7, "required": True, "channel_types": [0]}],
    },
]


class HiveBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.none())
        self.synced_once = False
        self.timer_task = None

    async def setup_hook(self):
        print("Bot setup complete. Waiting for Discord gateway connection...")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        if not self.synced_once:
            await self.register_commands()
            self.synced_once = True
        if not self.timer_task or self.timer_task.done():
            self.timer_task = asyncio.create_task(self.timer_loop())

    async def register_commands(self):
        token = config.get("token", "").strip()
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession(headers=headers) as session:
            # Remove stale global commands created by previous versions.
            async with session.put(f"{API}/applications/{self.user.id}/commands", json=[]) as resp:
                body = await resp.text()
                if resp.status not in (200, 201):
                    print(f"Warning: could not clear global commands: HTTP {resp.status} {body}")
                else:
                    print("Cleared old global slash commands.")

            # PUT replaces the complete guild command set, so the old broken
            # STRING version of /hivechannel cannot remain in these servers.
            for guild in self.guilds:
                url = f"{API}/applications/{self.user.id}/guilds/{guild.id}/commands"
                async with session.put(url, json=COMMANDS) as resp:
                    body = await resp.text()
                    if resp.status in (200, 201):
                        print(f"Synced {len(COMMANDS)} slash commands to {guild.name}.")
                    else:
                        print(f"Could not sync commands to {guild.name}: HTTP {resp.status} {body}")
        print(f"Finished syncing slash commands to {len(self.guilds)} server(s).")

    async def on_interaction(self, interaction: discord.Interaction):
        # Handle only application commands. No message-content intent is used.
        if interaction.type is not discord.InteractionType.application_command:
            return
        data_in = interaction.data or {}
        command_name = data_in.get("name")
        options = data_in.get("options", [])

        if command_name == "hivechannel":
            channel_id = get_option(options, "channel")
            if not channel_id:
                await interaction.response.send_message("❌ Please select a text channel.", ephemeral=True)
                return
            channel = interaction.guild.get_channel(int(channel_id)) if interaction.guild else None
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(channel_id))
                except Exception:
                    channel = None
            if channel is None or not isinstance(channel, discord.TextChannel):
                await interaction.response.send_message("❌ I could not find that text channel in this server.", ephemeral=True)
                return
            data["channel_id"] = channel.id
            save()
            await interaction.response.send_message(f"📢 **Hive channel set to {channel.mention}**")
            return

        if command_name == "hiverole":
            role_id = get_option(options, "role")
            if not role_id or not interaction.guild:
                await interaction.response.send_message("❌ Please select a role.", ephemeral=True)
                return
            # Fetch the guild roles directly so this works even with Intents.none()
            # and when the local role cache is empty.
            role = interaction.guild.get_role(int(role_id))
            if role is None:
                try:
                    roles = await interaction.guild.fetch_roles()
                    role = next((r for r in roles if r.id == int(role_id)), None)
                except Exception as exc:
                    print(f"Could not fetch roles: {exc}")
                    role = None
            if role is None:
                await interaction.response.send_message("❌ I could not find that role in this server. Make sure you selected a role from this server.", ephemeral=True)
                return
            data["role_id"] = role.id
            save()
            await interaction.response.send_message(f"🐝 **Hive ping role set to {role.mention}**")
            return

        if command_name != "hives":
            return

        sub = options[0].get("name") if options else None
        sub_options = options[0].get("options", []) if options else []

        if not await self.channel_ok(interaction):
            return

        if sub == "start":
            if not data.get("role_id"):
                await interaction.response.send_message("❌ Set a ping role first with `/hiverole`.", ephemeral=True)
                return
            if data.get("end_time"):
                await interaction.response.send_message("⚠️ The Hive timer is already running. Use `/hives reset` to restart it.", ephemeral=True)
                return
            raw_hours = get_option(sub_options, "hours")
            value = data.get("default_hours", 10) if raw_hours is None else parse_hours(raw_hours)
            if value is None:
                await interaction.response.send_message("❌ Enter a timer from **0.1 to 168 hours**.", ephemeral=True)
                return
            data["channel_id"] = interaction.channel_id
            data["duration_hours"] = value
            data["end_time"] = (datetime.now(timezone.utc) + timedelta(hours=value)).isoformat()
            data["warning_sent"] = False
            save()
            await interaction.response.send_message(
                f"🐝 **Blade Wasp Hive timer started!**\n⏱️ **{value:g} hours**\n🔔 **1 hour remaining** reminder\n🐝 **Refill** reminder when the timer ends"
            )
            return

        if sub == "reset":
            if not data.get("role_id"):
                await interaction.response.send_message("❌ Set a ping role first with `/hiverole`.", ephemeral=True)
                return
            raw_hours = get_option(sub_options, "hours")
            value = data.get("default_hours", 10) if raw_hours is None else parse_hours(raw_hours)
            if value is None:
                await interaction.response.send_message("❌ Enter a timer from **0.1 to 168 hours**.", ephemeral=True)
                return
            data["channel_id"] = interaction.channel_id
            data["duration_hours"] = value
            data["end_time"] = (datetime.now(timezone.utc) + timedelta(hours=value)).isoformat()
            data["warning_sent"] = False
            save()
            await interaction.response.send_message(f"🔄 **Hive timer reset!**\n⏱️ **{value:g} hours**")
            return

        if sub == "settime":
            raw_hours = get_option(sub_options, "hours")
            value = parse_hours(raw_hours)
            if value is None:
                await interaction.response.send_message("❌ Enter a default time from **0.1 to 168 hours**.", ephemeral=True)
                return
            data["default_hours"] = value
            save()
            await interaction.response.send_message(f"⚙️ **Default Hive timer set to {value:g} hours.**\nUse `/hives start` to use this default.")
            return

        if sub == "stop":
            data["end_time"] = None
            data["warning_sent"] = False
            save()
            await interaction.response.send_message("⏹️ **Hive timer stopped.**")
            return

        if sub == "status":
            if not data.get("end_time"):
                await interaction.response.send_message("⏹️ The Hive timer is **OFF**.")
                return
            left = (datetime.fromisoformat(data["end_time"]) - datetime.now(timezone.utc)).total_seconds()
            if left <= 0:
                await interaction.response.send_message("🐝 The timer has expired.")
                return
            total = math.ceil(left / 60)
            await interaction.response.send_message(f"🐝 **Blade Wasp Hive timer**\n⏱️ Remaining: **{total // 60}h {total % 60:02d}m**")
            return

    async def channel_ok(self, interaction):
        channel_id = data.get("channel_id")
        if channel_id is not None and interaction.channel_id != int(channel_id):
            await interaction.response.send_message("❌ This bot only works in the configured Hive channel.", ephemeral=True)
            return False
        return True

    async def timer_loop(self):
        while not self.is_closed():
            try:
                end_time = data.get("end_time")
                if end_time:
                    left = (datetime.fromisoformat(end_time) - datetime.now(timezone.utc)).total_seconds()
                    if 0 < left <= 3600 and not data.get("warning_sent", False):
                        await self.send_reminder("🐝 **Blade Wasp Hives: 1 hour remaining!**")
                        data["warning_sent"] = True
                        save()
                    if left <= 0:
                        await self.send_reminder("🐝 **BLADE WASP HIVES NEED REFILLING!**")
                        data["end_time"] = None
                        data["warning_sent"] = False
                        save()
            except Exception as exc:
                print(f"Timer error: {exc}")
            await asyncio.sleep(10)

    async def send_reminder(self, text):
        channel_id = data.get("channel_id")
        if not channel_id:
            return
        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except Exception as exc:
                print(f"Could not fetch Hive channel: {exc}")
                return
        role = None
        if data.get("role_id") and getattr(channel, "guild", None):
            role = channel.guild.get_role(int(data["role_id"]))
            if role is None:
                try:
                    role = await channel.guild.fetch_role(int(data["role_id"]))
                except Exception:
                    role = None
        message = (role.mention + " " if role else "") + text
        await channel.send(message, allowed_mentions=discord.AllowedMentions(roles=True))


bot = HiveBot()
token = config.get("token", "").strip()
if not token or token == "PASTE_YOUR_BOT_TOKEN_HERE":
    print("Open config.json and paste your Discord bot token.")
else:
    bot.run(token)
