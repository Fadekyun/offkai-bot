"""Private frontend-to-bot actions; never publish this port on the Docker host."""

import hmac
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import discord
from aiohttp import web

from offkai_bot.config import get_config
from offkai_bot.data.event import get_event
from offkai_bot.data.response import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EXTRA_PEOPLE,
    MAX_GUEST_NAME_LENGTH,
    get_responses,
    get_waitlist,
    promote_waitlist_response,
    update_attendee_names,
)
from offkai_bot.errors import (
    EventNotFoundError,
    MissingChannelIDError,
    RegistrationChangedError,
    ResponseNotFoundError,
    ThreadAccessError,
    ThreadNotFoundError,
)
from offkai_bot.event_actions import fetch_thread_for_event, update_event_message
from offkai_bot.interactions import (
    SignupContext,
    _add_user_to_event_thread,
    build_confirmation_message,
    credit_promoted_attendance,
    send_signup_reply,
)
from offkai_bot.payment_proof import payment_event_enabled
from offkai_bot.registration_removal import remove_registration
from offkai_bot.role_management import assign_event_role

_log = logging.getLogger(__name__)


async def parse_admin_request(request: web.Request, client: discord.Client) -> tuple[str, str] | web.Response:
    key = get_config().get("ADMIN_KEY", "")
    if not key or not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {key}"):
        return web.json_response({"error": "unauthorized"}, status=401)
    if not client.is_ready():
        return web.json_response({"error": "bot_unavailable"}, status=503)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "invalid_request"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_request"}, status=400)
    event_name, user_id = body.get("event_name"), body.get("user_id")
    if not isinstance(user_id, str) or not re.fullmatch(r"[0-9]{16,22}", user_id):
        return web.json_response({"error": "invalid_request"}, status=400)
    if not isinstance(event_name, str) or not event_name or len(event_name) > 100:
        return web.json_response({"error": "invalid_request"}, status=400)
    return event_name, user_id


def _parse_registration_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("Invalid timestamp")
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    timestamp = timestamp.astimezone(UTC)
    # The frontend displays and submits millisecond-precision ISO timestamps.
    return timestamp.replace(microsecond=timestamp.microsecond // 1000 * 1000)


def _parse_names_payload(
    body: dict,
) -> tuple[datetime, str | None, int, list[str | None], str | None, list[tuple[int, str]]]:
    if set(body) != {"event_name", "user_id", "registration_timestamp", "expected", "changes"}:
        raise ValueError("Unexpected request fields")
    expected_timestamp = _parse_registration_timestamp(body.get("registration_timestamp"))
    expected = body.get("expected")
    if not isinstance(expected, dict) or set(expected) != {"display_name", "extra_people", "extras_names"}:
        raise ValueError("Invalid expected snapshot")
    expected_display_name = expected["display_name"]
    expected_extra_people = expected["extra_people"]
    expected_extras_names = expected["extras_names"]
    if expected_display_name is not None and not isinstance(expected_display_name, str):
        raise ValueError("Invalid expected attendee name")
    if type(expected_extra_people) is not int or not (0 <= expected_extra_people <= MAX_EXTRA_PEOPLE):
        raise ValueError("Invalid expected guest count")
    if not isinstance(expected_extras_names, list) or len(expected_extras_names) < expected_extra_people:
        raise ValueError("Invalid expected guest names")
    for index, slot in enumerate(expected_extras_names):
        if index < expected_extra_people:
            if slot is not None and not isinstance(slot, str):
                raise ValueError("Invalid expected guest names")
        elif not isinstance(slot, str):
            raise ValueError("Invalid expected surplus guest names")

    changes = body.get("changes")
    if not isinstance(changes, dict) or not changes or not set(changes) <= {"display_name", "extras_names"}:
        raise ValueError("Invalid name changes")
    display_name = changes.get("display_name")
    if "display_name" in changes and (
        not isinstance(display_name, str) or len(display_name.strip()) > MAX_DISPLAY_NAME_LENGTH
    ):
        raise ValueError("Invalid attendee name")
    guest_changes = changes.get("extras_names", [])
    if not isinstance(guest_changes, list):
        raise ValueError("Invalid guest name changes")
    guest_names: list[tuple[int, str]] = []
    indices: set[int] = set()
    for change in guest_changes:
        if not isinstance(change, dict) or set(change) != {"index", "value"}:
            raise ValueError("Invalid guest name change")
        index, value = change["index"], change["value"]
        if type(index) is not int or not (0 <= index < expected_extra_people) or index in indices:
            raise ValueError("Invalid guest index")
        if not isinstance(value, str) or len(value.strip()) > MAX_GUEST_NAME_LENGTH:
            raise ValueError("Invalid guest name")
        indices.add(index)
        guest_names.append((index, value))
    if display_name is None and not guest_names:
        raise ValueError("No name changes supplied")
    return (
        expected_timestamp,
        expected_display_name,
        expected_extra_people,
        expected_extras_names,
        display_name,
        guest_names,
    )


def create_admin_app(client: discord.Client) -> web.Application:
    async def confirm_payment(request: web.Request) -> web.Response:
        parsed = await parse_admin_request(request, client)
        if isinstance(parsed, web.Response):
            return parsed
        event_name, user_id = parsed
        try:
            event = get_event(event_name)
        except EventNotFoundError:
            return web.json_response({"error": "not_found"}, status=404)
        if not payment_event_enabled(event):
            return web.json_response({"error": "not_found"}, status=404)
        entry = next(
            (
                entry
                for entry in [*get_responses(event.event_name), *get_waitlist(event.event_name)]
                if entry.user_id == int(user_id)
            ),
            None,
        )
        if entry is None:
            return web.json_response({"error": "not_found"}, status=404)
        if entry.payment_selection_demotes and entry.payment_method not in (event.signup_form or {}).get(
            "payment_methods", {}
        ):
            return web.json_response({"error": "payment_method_required"}, status=409)
        try:
            user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
            await user.send(f"Payment confirmed for {event_name}!")
        except discord.HTTPException:
            return web.json_response({"error": "dm_failed"}, status=502)
        return web.json_response({"sent": True})

    async def remove(request: web.Request) -> web.Response:
        parsed = await parse_admin_request(request, client)
        if isinstance(parsed, web.Response):
            return parsed
        event_name, user_id = parsed
        body = await request.json()
        timestamp = body.get("registration_timestamp")
        try:
            if not isinstance(timestamp, str) or len(timestamp) > 100:
                raise ValueError("Invalid timestamp")
            expected_timestamp = datetime.fromisoformat(timestamp)
            if expected_timestamp.tzinfo is None:
                raise ValueError("Timestamp requires timezone")
            expected_timestamp = expected_timestamp.astimezone(UTC)
            expected_timestamp = expected_timestamp.replace(microsecond=expected_timestamp.microsecond // 1000 * 1000)
        except ValueError:
            return web.json_response({"error": "invalid_request", "removed": False}, status=400)
        settings = get_config()
        try:
            event = get_event(event_name)
            if event.archived or event.interest_check:
                return web.json_response({"error": "event_unavailable"}, status=400)
            # Resolve the real guild/thread before changing registration data.
            thread = await fetch_thread_for_event(client, event)
            if thread.guild.id not in settings["GUILDS"]:
                return web.json_response({"error": "event_unavailable"}, status=403)
            user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
        except (EventNotFoundError, ThreadNotFoundError, discord.NotFound):
            return web.json_response({"error": "preflight_not_found", "removed": False}, status=404)
        except (ThreadAccessError, discord.Forbidden):
            return web.json_response({"error": "preflight_forbidden", "removed": False}, status=403)
        except Exception:
            _log.exception("Registration removal preflight failed")
            return web.json_response({"error": "preflight_unavailable", "removed": False}, status=503)
        try:
            result = await remove_registration(
                client,
                event,
                user,
                thread.guild,
                allow_waitlist=True,
                thread=thread,
                expected_timestamp=expected_timestamp,
            )
        except RegistrationChangedError:
            return web.json_response({"error": "registration_changed", "removed": False}, status=409)
        except (EventNotFoundError, ResponseNotFoundError):
            return web.json_response({"error": "not_found"}, status=404)
        except Exception:
            _log.exception("Admin registration removal failed for event %r", event_name)
            return web.json_response({"error": "removal_failed"}, status=502)
        try:
            await user.send(
                f"👋 Your attendance for **{event_name}** has been withdrawn by the organizer.\n"
                f"👋 主催者により**{event_name}**への参加が取り消されました。"
            )
        except discord.HTTPException:
            result["warnings"].append("The registration was removed, but the confirmation DM could not be delivered.")
        return web.json_response(result)

    async def promote(request: web.Request) -> web.Response:
        parsed = await parse_admin_request(request, client)
        if isinstance(parsed, web.Response):
            return parsed
        event_name, user_id = parsed
        body = await request.json()
        try:
            timestamp = body.get("registration_timestamp")
            if not isinstance(timestamp, str) or len(timestamp) > 100:
                raise ValueError("Invalid timestamp")
            expected_timestamp = datetime.fromisoformat(timestamp)
            if expected_timestamp.tzinfo is None:
                raise ValueError("Timestamp requires timezone")
            expected_timestamp = expected_timestamp.astimezone(UTC)
            expected_timestamp = expected_timestamp.replace(microsecond=expected_timestamp.microsecond // 1000 * 1000)
        except ValueError:
            return web.json_response({"error": "invalid_request", "promoted": False}, status=400)
        try:
            event = get_event(event_name)
            if event.archived or event.interest_check:
                return web.json_response({"error": "event_unavailable", "promoted": False}, status=400)
            thread = await fetch_thread_for_event(client, event)
            if thread.guild.id not in get_config()["GUILDS"]:
                return web.json_response({"error": "event_unavailable", "promoted": False}, status=403)
            user = client.get_user(int(user_id)) or await client.fetch_user(int(user_id))
        except (EventNotFoundError, ThreadNotFoundError, discord.NotFound):
            return web.json_response({"error": "preflight_not_found", "promoted": False}, status=404)
        except (ThreadAccessError, discord.Forbidden):
            return web.json_response({"error": "preflight_forbidden", "promoted": False}, status=403)
        except Exception:
            _log.exception("Registration promotion preflight failed")
            return web.json_response({"error": "preflight_unavailable", "promoted": False}, status=503)
        # Recheck after network preflight; no await between identity validation and commit.
        if event.archived or event.interest_check:
            return web.json_response({"error": "event_unavailable", "promoted": False}, status=400)
        try:
            entry = promote_waitlist_response(event, int(user_id), expected_timestamp=expected_timestamp)
            assert entry is not None
        except (RegistrationChangedError, ResponseNotFoundError):
            return web.json_response({"error": "registration_changed", "promoted": False}, status=409)
        except Exception:
            # A later metadata write can fail after the roster commit; do not claim no mutation.
            _log.exception("Admin registration promotion failed for event %r", event_name)
            return web.json_response({"error": "promotion_unconfirmed"}, status=502)
        warnings = []
        await credit_promoted_attendance(client, event, entry)
        # Saved attendance is not rolled back if Discord delivery or cleanup fails.
        actions: list[tuple[Callable[[], Awaitable[bool | None]], str]] = [
            (
                lambda: _add_user_to_event_thread(event, SignupContext(thread, thread.guild, user), client),
                "Promotion saved, but thread access could not be added. "
                "Check bot permissions and add the user manually.",
            ),
        ]
        role_id = event.role_id
        if role_id:
            actions.append(
                (
                    lambda: assign_event_role(thread.guild, entry.user_id, role_id, event=event),
                    "Promotion saved, but the participant role could not be assigned.",
                )
            )
        actions.extend(
            [
                (
                    lambda: send_signup_reply(
                        user.send,
                        event,
                        entry,
                        "Attendance confirmed",
                        build_confirmation_message(event, entry),
                        promotion=True,
                    ),
                    "Promotion saved, but the DM could not be delivered. Enable DMs and resend the confirmation.",
                ),
                (
                    lambda: update_event_message(client, event),
                    "Promotion saved, but the event message could not be updated.",
                ),
            ]
        )
        for action, warning in actions:
            try:
                if await action() is False:
                    warnings.append(warning)
            except Exception:
                _log.exception("Post-promotion action failed for user %s in event %r", user_id, event_name)
                warnings.append(warning)
        return web.json_response({"promoted": True, "warnings": warnings})

    async def rename(request: web.Request) -> web.Response:
        parsed = await parse_admin_request(request, client)
        if isinstance(parsed, web.Response):
            return parsed
        event_name, user_id = parsed
        body = await request.json()
        try:
            (
                expected_timestamp,
                expected_display_name,
                expected_extra_people,
                expected_extras_names,
                display_name,
                guest_names,
            ) = _parse_names_payload(body)
        except (ValueError, TypeError):
            return web.json_response({"error": "invalid_request", "saved": False}, status=400)
        settings = get_config()
        try:
            event = get_event(event_name)
            if event.archived or event.interest_check:
                return web.json_response({"error": "event_unavailable", "saved": False}, status=400)
            # Resolve the real guild/thread before changing registration data.
            thread = await fetch_thread_for_event(client, event)
            if thread.guild.id not in settings["GUILDS"]:
                return web.json_response({"error": "event_unavailable", "saved": False}, status=403)
        except (EventNotFoundError, MissingChannelIDError, ThreadNotFoundError, discord.NotFound):
            return web.json_response({"error": "preflight_not_found", "saved": False}, status=404)
        except (ThreadAccessError, discord.Forbidden):
            return web.json_response({"error": "preflight_forbidden", "saved": False}, status=403)
        except Exception:
            _log.exception("Admin name update preflight failed")
            return web.json_response({"error": "preflight_unavailable", "saved": False}, status=503)
        # Recheck availability after the network preflight. The final identity and
        # snapshot validation and atomic commit below remain synchronous.
        try:
            event = get_event(event_name)
        except EventNotFoundError:
            return web.json_response({"error": "not_found", "saved": False}, status=404)
        if event.archived or event.interest_check or event.thread_id != thread.id:
            return web.json_response({"error": "event_unavailable", "saved": False}, status=400)
        try:
            updated = update_attendee_names(
                event.event_name,
                int(user_id),
                expected_timestamp,
                expected_display_name,
                expected_extra_people,
                expected_extras_names,
                display_name=display_name,
                guest_names=guest_names,
            )
        except RegistrationChangedError:
            return web.json_response({"error": "registration_changed", "saved": False}, status=409)
        except ValueError:
            return web.json_response({"error": "invalid_request", "saved": False}, status=400)
        except Exception:
            _log.exception("Admin name update failed for event %r", event_name)
            return web.json_response({"error": "write_failed", "saved": False}, status=500)
        return web.json_response(
            {
                "saved": True,
                "registration": {
                    "user_id": str(updated.user_id),
                    "registration_timestamp": expected_timestamp.isoformat(),
                    "display_name": updated.display_name,
                    "extra_people": updated.extra_people,
                    "extras_names": list(updated.extras_names),
                },
            }
        )

    app = web.Application(client_max_size=4096)
    app.router.add_post("/registrations/remove", remove)
    app.router.add_post("/registrations/promote", promote)
    app.router.add_post("/registrations/names", rename)
    app.router.add_post("/payments/confirm", confirm_payment)
    return app


async def start_admin_api(client: discord.Client) -> web.AppRunner | None:
    port = int(os.environ.get("ADMIN_API_PORT", "0"))
    if not port:
        return None
    runner = web.AppRunner(create_admin_app(client), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, os.environ.get("ADMIN_API_HOST", "127.0.0.1"), port).start()
    except Exception:
        await runner.cleanup()
        raise
    return runner
