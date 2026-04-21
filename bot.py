"""
bot.py — Discord bot: slash commands + notification delivery.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
import db
from monitor import scheduler
from utils import (
    embed_account_down,
    embed_account_restored,
    embed_analytics,
    embed_error,
    embed_list,
    embed_status,
    embed_success,
    setup_logging,
)

setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def _valid_username(username: str) -> bool:
    return bool(_USERNAME_RE.match(username))


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------
class InstaMonitorBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await db.get_conn()         # init DB
        await self.tree.sync()      # sync slash commands globally
        scheduler.set_notify_callback(_notify_users)
        await scheduler.start()
        logger.info("Bot setup complete — slash commands synced.")

    async def on_ready(self) -> None:
        assert self.user is not None
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Instagram accounts",
            )
        )

    async def close(self) -> None:
        await scheduler.stop()
        await db.close()
        await super().close()


bot = InstaMonitorBot()


# ---------------------------------------------------------------------------
# Notification callback (called by monitor.py)
# ---------------------------------------------------------------------------
async def _notify_users(username: str, event_type: str, user_ids: list[int]) -> None:
    embed = embed_account_restored(username) if event_type == "restored" else embed_account_down(username)
    for uid in user_ids:
        try:
            user = await bot.fetch_user(uid)
            await user.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot DM user %s (DMs disabled?)", uid)
        except discord.NotFound:
            logger.warning("User %s not found.", uid)
        except Exception:
            logger.exception("Failed to notify user %s about @%s", uid, username)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
@bot.tree.command(name="track", description="Start monitoring an Instagram account.")
@app_commands.describe(username="Instagram username to monitor (without @)")
async def cmd_track(interaction: discord.Interaction, username: str) -> None:
    await interaction.response.defer(ephemeral=True)
    username = username.lstrip("@").strip().lower()

    if not _valid_username(username):
        await interaction.followup.send(
            embed=embed_error("Invalid username. Use only letters, numbers, dots, and underscores (max 30 chars)."),
            ephemeral=True,
        )
        return

    count = await db.count_tracked(interaction.user.id)
    if count >= config.MAX_ACCOUNTS_PER_USER:
        await interaction.followup.send(
            embed=embed_error(f"You've reached the limit of **{config.MAX_ACCOUNTS_PER_USER}** tracked accounts."),
            ephemeral=True,
        )
        return

    added = await db.add_tracking(interaction.user.id, username)
    if added:
        await interaction.followup.send(
            embed=embed_success(f"Now monitoring **@{username}**.\nYou'll receive a DM when its status changes."),
            ephemeral=True,
        )
        logger.info("User %s started tracking @%s", interaction.user.id, username)
    else:
        await interaction.followup.send(
            embed=embed_error(f"You're already tracking **@{username}**."),
            ephemeral=True,
        )


@bot.tree.command(name="untrack", description="Stop monitoring an Instagram account.")
@app_commands.describe(username="Instagram username to stop monitoring")
async def cmd_untrack(interaction: discord.Interaction, username: str) -> None:
    await interaction.response.defer(ephemeral=True)
    username = username.lstrip("@").strip().lower()

    removed = await db.remove_tracking(interaction.user.id, username)
    if removed:
        await interaction.followup.send(
            embed=embed_success(f"Stopped monitoring **@{username}**."),
            ephemeral=True,
        )
        logger.info("User %s untracked @%s", interaction.user.id, username)
    else:
        await interaction.followup.send(
            embed=embed_error(f"You weren't tracking **@{username}**."),
            ephemeral=True,
        )


@bot.tree.command(name="list", description="Show all Instagram accounts you're monitoring.")
async def cmd_list(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    rows = await db.list_tracked(interaction.user.id)
    await interaction.followup.send(
        embed=embed_list(rows, interaction.user),
        ephemeral=True,
    )


@bot.tree.command(name="status", description="Check the current status of an Instagram account.")
@app_commands.describe(username="Instagram username to check")
async def cmd_status(interaction: discord.Interaction, username: str) -> None:
    await interaction.response.defer(ephemeral=True)
    username = username.lstrip("@").strip().lower()

    row = await db.get_account_status(username)
    if row:
        await interaction.followup.send(embed=embed_status(row), ephemeral=True)
    else:
        await interaction.followup.send(
            embed=embed_error(
                f"**@{username}** is not being tracked by anyone yet.\n"
                f"Use `/track {username}` to start monitoring."
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
def _is_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id in config.ADMIN_USER_IDS


@bot.tree.command(name="broadcast", description="[Admin] Send a message to all tracked users.")
@app_commands.describe(message="Message to broadcast")
async def cmd_broadcast(interaction: discord.Interaction, message: str) -> None:
    await interaction.response.defer(ephemeral=True)

    if not _is_admin(interaction):
        await interaction.followup.send(embed=embed_error("You don't have permission to use this command."), ephemeral=True)
        return

    db_conn = await db.get_conn()
    cursor = await db_conn.execute("SELECT user_id FROM users")
    rows = await cursor.fetchall()
    user_ids = [r[0] for r in rows]

    embed = discord.Embed(
        title="📢 Broadcast",
        description=message,
        colour=discord.Colour.orange(),
    )
    embed.set_footer(text="Instagram Monitor — Admin Broadcast")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            user = await bot.fetch_user(uid)
            await user.send(embed=embed)
            sent += 1
        except Exception:
            failed += 1

    await interaction.followup.send(
        embed=embed_success(f"Broadcast sent to **{sent}** users ({failed} failed)."),
        ephemeral=True,
    )


@bot.tree.command(name="analytics", description="[Admin] View monitoring statistics.")
async def cmd_analytics(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not _is_admin(interaction):
        await interaction.followup.send(embed=embed_error("You don't have permission to use this command."), ephemeral=True)
        return

    stats = await db.global_stats()
    await interaction.followup.send(embed=embed_analytics(stats), ephemeral=True)


@bot.tree.command(name="history", description="View recent status change events for an account.")
@app_commands.describe(username="Instagram username", limit="Number of events (max 20)")
async def cmd_history(interaction: discord.Interaction, username: str, limit: int = 10) -> None:
    await interaction.response.defer(ephemeral=True)
    username = username.lstrip("@").strip().lower()
    limit = max(1, min(limit, 20))

    events = await db.get_events(username, limit=limit)
    if not events:
        await interaction.followup.send(
            embed=embed_error(f"No events found for **@{username}**."),
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"📜 Event History — @{username}",
        colour=discord.Colour.blurple(),
    )
    lines = []
    for ev in events:
        etype = ev["event_type"]
        emoji = {"down": "🔴", "restored": "🟢", "error": "⚠️", "check": "🔵"}.get(etype, "•")
        lines.append(f"{emoji} `{ev['created_at']}` — **{etype}** {ev.get('detail','')}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Instagram Monitor")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set. Check your .env file.")
    bot.run(config.DISCORD_TOKEN, log_handler=None)
