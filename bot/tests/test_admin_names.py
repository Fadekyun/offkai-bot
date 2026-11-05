"""Authenticated admin name-edit endpoint and its conflict/security boundaries."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from offkai_bot import admin_api
from offkai_bot.data.event import Event
from offkai_bot.data.response import (
    Response,
    WaitlistEntry,
    add_response,
    add_to_waitlist,
    get_responses,
    get_waitlist,
    load_responses,
    remove_response,
)

NOW = datetime(2026, 10, 1, 12, 0, 0, 123000, tzinfo=UTC)
UID = 101000000000000001
HEADERS = {"Authorization": "Bearer synthetic-admin"}


def make_event(**overrides) -> Event:
    values = {
        "event_name": "Names endpoint",
        "venue": "Venue",
        "address": "Address",
        "google_maps_link": "",
        "event_datetime": datetime.now(UTC) + timedelta(days=7),
        "thread_id": 999,
    }
    values.update(overrides)
    return Event(**values)


def make_attendee(**overrides) -> Response:
    values = {
        "user_id": UID,
        "username": "discord-name",
        "extra_people": 2,
        "behavior_confirmed": True,
        "arrival_confirmed": True,
        "event_name": "Names endpoint",
        "timestamp": NOW,
        "drinks": ["Tea", "Cola", "Water"],
        "extras_names": ["Alice", "Bob"],
        "display_name": "Before",
        "payment_method": "Bank",
        "attendee_number": 4,
        "extras_attendee_numbers": [5, 6],
    }
    values.update(overrides)
    return Response(**values)


def payload(event: Event, current: Response, **overrides):
    body = {
        "event_name": event.event_name,
        "user_id": str(UID),
        "registration_timestamp": current.timestamp.isoformat(),
        "expected": {
            "display_name": current.display_name,
            "extra_people": current.extra_people,
            "extras_names": [
                current.extras_names[index] if index < len(current.extras_names) else None
                for index in range(current.extra_people)
            ]
            + list(current.extras_names[current.extra_people :]),
        },
        "changes": {"display_name": "After"},
    }
    body.update(overrides)
    return body


@pytest.fixture
def names_setup(monkeypatch, mock_paths):
    Path(mock_paths["responses"]).write_text("{}")
    load_responses()
    event = make_event()
    client = MagicMock(spec=discord.Client)
    client.is_ready.return_value = True
    thread = MagicMock(spec=discord.Thread)
    thread.id = event.thread_id
    thread.guild.id = 555
    client.get_channel.return_value = thread
    monkeypatch.setattr(admin_api, "get_config", lambda: {"ADMIN_KEY": "synthetic-admin", "GUILDS": [555]})
    monkeypatch.setattr(admin_api, "get_event", lambda name: event)
    return event, client, Path(mock_paths["responses"])


async def post_names(client, event, body, *, headers=HEADERS):
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post("/registrations/names", json=body, headers=headers)
        return reply.status, await reply.json()


async def test_name_edit_roundtrip_persists_only_names_and_has_no_discord_side_effects(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)

    body = payload(
        event,
        current,
        changes={
            "display_name": "  After  ",
            "extras_names": [{"index": 0, "value": "  Alicia  "}],
        },
    )
    status, result = await post_names(client, event, body)

    assert status == 200
    assert result == {
        "saved": True,
        "registration": {
            "user_id": str(UID),
            "registration_timestamp": NOW.isoformat(),
            "display_name": "After",
            "extra_people": 2,
            "extras_names": ["Alicia", "Bob"],
        },
    }
    saved = get_responses(event.event_name)[0]
    assert saved.display_name == "After"
    assert saved.extras_names == ["Alicia", "Bob"]
    assert saved.payment_method == "Bank"
    assert saved.attendee_number == 4
    assert saved.extras_attendee_numbers == [5, 6]
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk[event.event_name]["attendees"][0]["extras_names"] == ["Alicia", "Bob"]
    client.fetch_user.assert_not_called()
    client.get_user.assert_not_called()
    client.fetch_channel.assert_not_called()


@pytest.mark.parametrize(
    "body_change",
    [
        {"unexpected": True},
        {"registration_timestamp": "not-a-timestamp"},
        {"registration_timestamp": "2026-10-01T12:00:00"},
        {"expected": {"display_name": "Before", "extra_people": 2, "extras_names": ["Alice"]}},
        {"expected": {"display_name": "Before", "extra_people": 2, "extras_names": ["Alice", "Bob"], "extra": 1}},
        {"changes": {}},
        {"changes": {"display_name": "   "}},
        {"changes": {"display_name": "x" * 33}},
        {"changes": {"extras_names": [{"index": 2, "value": "Out of range"}]}},
        {"changes": {"extras_names": [{"index": 0, "value": "One"}, {"index": 0, "value": "Again"}]}},
        {"changes": {"extras_names": [{"index": 0, "value": "   "}]}},
        {"changes": {"unknown": "value"}},
    ],
)
async def test_malformed_requests_are_rejected_without_mutation(names_setup, body_change):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    cache = get_responses(event.event_name)
    status, result = await post_names(client, event, {**payload(event, current), **body_change})
    assert status == 400
    assert result == {"error": "invalid_request", "saved": False}
    assert get_responses(event.event_name) is cache
    assert get_responses(event.event_name)[0] is current
    assert path.read_bytes() == disk


async def test_authentication_and_event_guards(names_setup, monkeypatch):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)

    assert (await post_names(client, event, body, headers={}))[0] == 401
    assert (await post_names(client, event, body, headers={"Authorization": "Bearer wrong"}))[0] == 401

    monkeypatch.setattr(admin_api, "get_event", lambda name: make_event(archived=True))
    assert (await post_names(client, event, body)) == (
        400,
        {"error": "event_unavailable", "saved": False},
    )
    monkeypatch.setattr(admin_api, "get_event", lambda name: make_event(interest_check=True))
    assert (await post_names(client, event, body)) == (
        400,
        {"error": "event_unavailable", "saved": False},
    )

    def missing(name):
        raise admin_api.EventNotFoundError(name)

    monkeypatch.setattr(admin_api, "get_event", missing)
    assert (await post_names(client, event, body)) == (
        404,
        {"error": "preflight_not_found", "saved": False},
    )

    monkeypatch.setattr(admin_api, "get_event", lambda name: event)
    thread = MagicMock(spec=discord.Thread)
    thread.guild.id = 999
    client.get_channel.return_value = thread
    assert (await post_names(client, event, body)) == (
        403,
        {"error": "event_unavailable", "saved": False},
    )
    assert get_responses(event.event_name)[0].display_name == "Before"


async def test_preflight_fails_closed_for_cache_miss_wrong_guild_and_missing_thread(names_setup, monkeypatch):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)

    fetched = MagicMock(spec=discord.Thread)
    fetched.id = event.thread_id
    fetched.guild.id = 555
    client.get_channel.return_value = None
    client.fetch_channel = AsyncMock(return_value=fetched)
    status, result = await post_names(client, event, body)
    assert status == 200
    assert result["saved"] is True
    client.fetch_channel.assert_awaited_once_with(event.thread_id)

    body = payload(event, make_attendee(display_name="After"))
    fetched.guild.id = 999
    assert (await post_names(client, event, body)) == (
        403,
        {"error": "event_unavailable", "saved": False},
    )

    monkeypatch.setattr(admin_api, "get_event", lambda name: make_event(thread_id=None))
    assert (await post_names(client, event, payload(event, current))) == (
        404,
        {"error": "preflight_not_found", "saved": False},
    )

    monkeypatch.setattr(admin_api, "get_event", lambda name: event)
    client.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "Forbidden"))
    assert (await post_names(client, event, payload(event, current))) == (
        403,
        {"error": "preflight_forbidden", "saved": False},
    )
    assert get_responses(event.event_name)[0].display_name == "After"


async def test_event_availability_is_rechecked_after_the_awaited_preflight(names_setup, monkeypatch):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    thread = MagicMock(spec=discord.Thread)
    thread.id = event.thread_id
    thread.guild.id = 555

    async def archive_during_preflight(*args):
        event.archived = True
        return thread

    monkeypatch.setattr(admin_api, "fetch_thread_for_event", archive_during_preflight)
    assert (await post_names(client, event, payload(event, current))) == (
        400,
        {"error": "event_unavailable", "saved": False},
    )
    assert get_responses(event.event_name)[0].display_name == "Before"


@pytest.mark.parametrize("stale_kind", ["rejoin", "display_name", "guest_count", "guest_slot", "waitlist"])
async def test_stale_or_replaced_registrations_conflict_without_overwrite(names_setup, stale_kind):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)

    if stale_kind == "rejoin":
        remove_response(event.event_name, UID)
        add_response(event.event_name, make_attendee(timestamp=NOW + timedelta(seconds=1)))
    elif stale_kind == "display_name":
        current.display_name = "Changed elsewhere"
    elif stale_kind == "guest_count":
        current.extra_people = 1
        current.extras_names = ["Alice"]
    elif stale_kind == "guest_slot":
        current.extras_names = ["Alice", "Changed"]
    elif stale_kind == "waitlist":
        remove_response(event.event_name, UID)
        waitlist_fields = WaitlistEntry.__dataclass_fields__
        add_to_waitlist(
            event.event_name,
            WaitlistEntry(**{key: value for key, value in current.__dict__.items() if key in waitlist_fields}),
        )

    status, result = await post_names(client, event, body)
    assert status == 409
    assert result == {"error": "registration_changed", "saved": False}
    if stale_kind == "waitlist":
        assert get_responses(event.event_name) == []
        assert get_waitlist(event.event_name)[0].display_name == "Before"
    else:
        saved = get_responses(event.event_name)[0]
        assert saved.display_name != "After"


async def test_second_admin_save_conflicts_and_first_remains(names_setup):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)

    assert (await post_names(client, event, body))[0] == 200
    assert get_responses(event.event_name)[0].display_name == "After"
    status, result = await post_names(client, event, {**body, "changes": {"display_name": "Second"}})
    assert status == 409
    assert result == {"error": "registration_changed", "saved": False}
    assert get_responses(event.event_name)[0].display_name == "After"


async def test_disk_write_failure_returns_error_and_preserves_cache_and_disk(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    cache = get_responses(event.event_name)
    with patch("offkai_bot.data.response.atomic_write_json", side_effect=OSError("disk unavailable")):
        status, result = await post_names(client, event, payload(event, current))
    assert status == 500
    assert result == {"error": "write_failed", "saved": False}
    assert get_responses(event.event_name) is cache
    assert get_responses(event.event_name)[0] is current
    assert current.display_name == "Before"
    assert path.read_bytes() == disk
