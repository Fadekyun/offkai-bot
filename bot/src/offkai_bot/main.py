import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

# --- Updated Imports ---
from offkai_bot import config
from offkai_bot.alerts.alerts import alert_loop
from offkai_bot.alerts.reminders import register_checkin_reminder, register_deadline_reminders
from offkai_bot.config import get_config

# Import only necessary data loaders for initial cache population
from offkai_bot.data.event import load_event_data
from offkai_bot.data.ranking import load_rankings
from offkai_bot.data.response import load_responses
from offkai_bot.errors import (
    BotCommandError,
    PinPermissionError,  # Import the error for handling
)
from offkai_bot.event_actions import (
    fetch_thread_for_event,
    update_event_message,
)

# --- End Updated Imports ---

_log = logging.getLogger(__name__)
settings: dict[str, Any] = {}
LOG_FORMAT = "%(asctime)s:%(levelname)s:%(name)s: %(message)s"
DEFAULT_LOG_FILE = "logs/offkai-bot.log"
_OFFKAI_LOG_HANDLER_ATTR = "_offkai_bot_managed_handler"


async def load_and_update_events(client: discord.Client):
    """Loads events on startup and ensures their messages/views are up-to-date."""
    _log.info("Loading and updating event messages...")
    events = load_event_data()
    if not events:
        _log.info("No events found to load.")
        return

    for event in events:
        if not event.archived:
            try:
                # Pass only the client and event
                await update_event_message(client, event)

                # Register check-in reminder first — it needs no thread, so a
                # fetch_thread_for_event failure below cannot prevent it from running.
                register_checkin_reminder(client, event)

                # Register deadline close alerts (requires thread).
                thread = await fetch_thread_for_event(client, event)
                register_deadline_reminders(client, event, thread)
            except Exception:
                _log.exception("Failed to process event %r at startup; skipping.", event.event_name)

    _log.info("Finished loading and updating event messages.")


class OffkaiClient(commands.Bot):
    def __init__(self, *, intents: discord.Intents):
        # Initialize commands.Bot. Command prefix is required but we only use slash commands.
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        # Load extensions (Cogs)
        try:
            await self.load_extension("offkai_bot.cogs.general")
            await self.load_extension("offkai_bot.cogs.events")
            _log.info("Extensions loaded.")
        except Exception as e:
            _log.exception("Failed to load extensions: %s", e)
            raise e

        # Load Data
        load_event_data()
        load_responses()  # Loads both attendees and waitlist
        load_rankings()
        _log.info("Initial data loaded into cache.")

        if not get_config().get("FRONTEND_URL"):
            _log.warning("FRONTEND_URL is not set — check-in URLs will be omitted from DMs.")

        # Sync commands
        for guild_id in settings["GUILDS"]:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        _log.info("Commands synced.")

        await load_and_update_events(self)
        alert_loop.start()


intents = discord.Intents.default()
intents.message_content = True

client = OffkaiClient(intents=intents)


# --- Error Handler ---


@client.tree.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Handles application command errors globally."""
    user_info = f"User: {interaction.user} ({interaction.user.id})"
    command_name = interaction.command.name if interaction.command else "Unknown"

    # First, handle discord.py's specific check failures directly from 'error'
    match error:
        case app_commands.MissingRole():
            message = "❌ You need the Offkai Organizer role to use this command."
            _log.warning("%s - Missing Offkai Organizer role for command '%s'.", user_info, command_name)
            await interaction.response.send_message(message, ephemeral=True)
            return  # Handled

        case app_commands.CheckFailure():
            message = "❌ You do not have permission to use this command."
            _log.warning("%s - CheckFailure for command '%s'.", user_info, command_name)
            await interaction.response.send_message(message, ephemeral=True)
            return  # Handled

    # For other errors, work with the 'original' error if it exists
    original_error = getattr(error, "original", error)
    message = ""

    # Now, match against the original error type
    match original_error:
        # --- Handle PinPermissionError gracefully ---
        # This error is not critical; the command succeeded but the pin failed.
        # We send a followup instead of the standard error message.
        case PinPermissionError() as e:
            log_level = getattr(e, "log_level", logging.WARNING)
            _log.log(log_level, "%s - Handled (%s): %s", user_info, type(e).__name__, e)
            # The initial response was already sent by the command, so we use a followup
            await interaction.followup.send(str(e), ephemeral=True)
            return  # Handled

        # --- Unified Case for other custom errors ---
        case BotCommandError() as e:
            message = str(e)
            log_level = getattr(e, "log_level", logging.INFO)
            _log.log(log_level, "%s - Handled (%s): %s", user_info, type(e).__name__, message)

        # --- Specific Discord Errors (Keep separate) ---
        case discord.Forbidden():
            message = "❌ The bot lacks permissions to perform this action."
            _log.warning("%s - Encountered discord.Forbidden for command '%s'.", user_info, command_name)

        # --- Default Case for Unhandled Errors (Keep separate) ---
        case _:
            _log.error(
                "%s - Unhandled command error for '%s': %s",
                user_info,
                command_name,
                error,
                exc_info=original_error,
            )
            message = "❌ An unexpected error occurred. Please try again later or contact an admin."

    # Send the response (if a message was set)
    if message:
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except discord.HTTPException as http_err:
            _log.error("%s - Failed to send error response message: %s", user_info, http_err)
        except Exception as e:
            _log.error(
                "%s - Exception sending error response message: %s",
                user_info,
                e,
                exc_info=e,
            )


# Event to run when the client is ready
@client.event
async def on_ready():
    _log.info("Logged in as %s", client.user)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offkai Bot")
    parser.add_argument("--config-path", type=str, default="config.json")
    return parser.parse_args()


def configure_logging(log_file: str | None = DEFAULT_LOG_FILE) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    for handler in root_logger.handlers[:]:
        if getattr(handler, _OFFKAI_LOG_HANDLER_ATTR, False):
            root_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _OFFKAI_LOG_HANDLER_ATTR, True)
    root_logger.addHandler(stream_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        setattr(file_handler, _OFFKAI_LOG_HANDLER_ATTR, True)
        root_logger.addHandler(file_handler)

    discord_logger = logging.getLogger("discord")
    discord_logger.setLevel(logging.WARNING)  # Reduce discord lib noise


def main() -> None:
    global settings
    args = parse_args()

    # Setup logging
    configure_logging(log_file=None)

    try:
        # Explicitly load the configuration ONCE at startup
        config.load_config(args.config_path)
    except config.ConfigError as e:
        _log.critical("Fatal Error: Failed to load configuration - %s", e)
        sys.exit(1)

    # Now access the config via the accessor function
    settings = config.get_config()
    log_file = settings.get("LOG_FILE", DEFAULT_LOG_FILE)
    configure_logging(log_file)
    if log_file:
        _log.info("Logging to %s", log_file)

    # Validate config before running
    if not settings["DISCORD_TOKEN"]:
        _log.critical("DISCORD_TOKEN is not set")
    elif not settings["GUILDS"]:
        _log.critical("GUILDS is not set")
    else:
        try:
            client.run(settings["DISCORD_TOKEN"], log_handler=None)  # Use basicConfig handler
        except Exception as e:
            _log.exception("Fatal error running bot: %s", e)
