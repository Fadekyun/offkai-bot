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
    compute_names_revision,
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


def payload(event: Event, current: Response, *, revision: str | None = None, **overrides):
    body = {
        "event_name": event.event_name,
        "user_id": str(UID),
        "registration_timestamp": current.timestamp.isoformat(),
        "expected_revision": revision
        or compute_names_revision(current.display_name, current.extra_people, current.extras_names),
        "changes": {"display_name": "After"},
    }
    body.update(overrides)
    return body


def name_parts(changes: object) -> list[tuple[str, str]]:
    if not isinstance(changes, dict):
        return []
    parts: list[tuple[str, str]] = []
    if isinstance(changes.get("display_name"), str):
        parts.append(("display_name", changes["display_name"]))
    guest_changes = changes.get("extras_names")
    if isinstance(guest_changes, list):
        for change in guest_changes:
            if isinstance(change, dict) and type(change.get("index")) is int and isinstance(change.get("value"), str):
                parts.append((f"guest:{change['index']}", change["value"]))
    return parts


def request_body(event: Event, body: dict) -> dict:
    changes = body.get("changes")
    request = {
        "event_name": body["event_name"],
        "user_id": body["user_id"],
        "registration_timestamp": body["registration_timestamp"],
        "expected_revision": body["expected_revision"],
    }
    if isinstance(changes, dict):
        if "display_name" in changes:
            request["display_name"] = changes["display_name"]
        guest_names = {}
        for field, value in name_parts(changes):
            if field != "display_name":
                guest_names[field.split(":", 1)[1]] = value
        if guest_names:
            request["guest_names"] = guest_names
        for key, value in changes.items():
            if key not in {"display_name", "extras_names"}:
                request[key] = value
    for key, value in body.items():
        if key not in {"event_name", "user_id", "registration_timestamp", "expected_revision", "changes"}:
            request[key] = value
    return request


async def save_names(http, event: Event, body: dict, *, sizes: list[int] | None = None):
    encoded = json.dumps(request_body(event, body), ensure_ascii=False, separators=(",", ":"))
    try:
        raw = encoded.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates cannot be literal UTF-8; send them as JSON escapes so
        # the bot still has to reject the decoded invalid value.
        raw = json.dumps(request_body(event, body), ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if sizes is not None:
        sizes.append(len(raw))
    reply = await http.post("/registrations/names", data=raw, headers=HEADERS)
    return reply.status, await reply.json()


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
    user = MagicMock(spec=discord.User, send=AsyncMock())
    client.get_user.return_value = user
    client.fetch_user = AsyncMock(return_value=user)
    monkeypatch.setattr(admin_api, "get_config", lambda: {"ADMIN_KEY": "synthetic-admin", "GUILDS": [555]})
    monkeypatch.setattr(admin_api, "get_event", lambda name: event)
    return event, client, Path(mock_paths["responses"])


async def post_names(client, event, body):
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        return await save_names(http, event, body)


async def test_name_edit_persists_before_sending_one_changed_fields_dm(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    user = client.get_user.return_value
    sent = []

    async def send(message, **kwargs):
        sent.append((message, kwargs))
        # The disk and cache must already contain the committed names.
        assert get_responses(event.event_name)[0].display_name == "After"
        assert get_responses(event.event_name)[0].extras_names == ["Alicia", "Bob"]
        disk = json.loads(path.read_text(encoding="utf-8"))
        assert disk[event.event_name]["attendees"][0]["display_name"] == "After"
        assert disk[event.event_name]["attendees"][0]["extras_names"] == ["Alicia", "Bob"]

    user.send.side_effect = send
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
        "warnings": [],
        "registration": {
            "user_id": str(UID),
            "registration_timestamp": NOW.isoformat(),
            "display_name": "After",
            "extra_people": 2,
            "extras_names": ["Alicia", "Bob"],
            "names_revision": "1107b635afe1f73d87b88669ec352d86a9d7a87ee1c9487c317ee606a878b24f",
        },
    }
    assert len(sent) == 1
    message, kwargs = sent[0]
    assert message == (
        "✏️ The organizer changed your registration for **Names endpoint**:\n"
        "👤 Name: ~~Before~~ → **After**\n"
        "👥 Guest 1: ~~Alice~~ → **Alicia**\n"
        "\n"
        "✏️ 主催者が**Names endpoint**の登録内容を変更しました：\n"
        "👤 名前：~~Before~~ → **After**\n"
        "👥 同伴者1：~~Alice~~ → **Alicia**"
    )
    allowed = kwargs["allowed_mentions"]
    assert allowed.users is False and allowed.roles is False and allowed.everyone is False
    client.get_user.assert_called_once_with(UID)
    client.fetch_user.assert_not_called()
    user.send.assert_awaited_once()
    client.fetch_channel.assert_not_called()

    saved = get_responses(event.event_name)[0]
    assert saved.payment_method == "Bank"
    assert saved.attendee_number == 4
    assert saved.extras_attendee_numbers == [5, 6]


async def post_names_raw(client, raw: bytes):
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post("/registrations/names", data=raw, headers=HEADERS)
        return reply.status, await reply.json()


@pytest.mark.parametrize("guest_count", range(6))
async def test_max_length_multibyte_names_fit_one_request(names_setup, guest_count):
    event, client, path = names_setup
    old_display = "名😀" * 16
    new_display = "新😀" * 16
    old_guests = ["客😀" * 80 for _ in range(guest_count)]
    new_guests = ["伴😀" * 80 for _ in range(guest_count)]
    current = make_attendee(
        display_name=old_display,
        extra_people=guest_count,
        extras_names=old_guests,
        extras_attendee_numbers=list(range(5, 5 + guest_count)),
    )
    add_response(event.event_name, current)
    changes = {"display_name": new_display}
    if guest_count:
        changes["extras_names"] = [{"index": index, "value": value} for index, value in enumerate(new_guests)]
    body = payload(event, current, changes=changes)
    sizes: list[int] = []
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        status, result = await save_names(http, event, body, sizes=sizes)

    assert status == 200, result
    assert result["saved"] is True
    assert result["warnings"] == []
    assert len(sizes) == 1
    assert sizes[0] <= 4096, sizes
    saved = get_responses(event.event_name)[0]
    assert saved.display_name == new_display
    assert saved.extras_names == new_guests
    assert json.loads(path.read_text(encoding="utf-8"))[event.event_name]["attendees"][0]["display_name"] == new_display
    assert client.get_user.return_value.send.await_count == 1


async def test_maximum_single_request_fits_the_4k_limit():
    # The largest changed-name payload: 32/160 codepoints of 4-byte UTF-8, plus
    # the maximum event, timestamp, user and revision fields. JSON.stringify and
    # json.dumps(ensure_ascii=False) both send these literal characters as UTF-8.
    emoji = "😀"
    body = {
        "event_name": emoji * 100,
        "user_id": "1" * 22,
        "registration_timestamp": "2" * 100,
        "expected_revision": "a" * 64,
        "display_name": emoji * 32,
        "guest_names": {str(index): emoji * 160 for index in range(5)},
    }
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(raw) <= 4096
    assert len(raw) == 4064


async def test_streamed_body_over_4k_is_rejected_without_usable_content_length(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    body = payload(event, current, changes={"display_name": "After"})
    base = json.dumps(
        request_body(event, body),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    raw = base + b" " * (4097 - len(base))

    async def chunks():
        for offset in range(0, len(raw), 500):
            yield raw[offset : offset + 500]

    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post("/registrations/names", data=chunks(), headers=HEADERS)
        result = await reply.json()
    assert reply.status == 413
    assert result == {"error": "request_too_large", "saved": False}
    assert get_responses(event.event_name)[0].display_name == "Before"
    assert path.read_bytes() == disk
    client.get_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()


async def test_other_admin_routes_retain_the_4k_limit(names_setup):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    base = json.dumps(
        {"event_name": event.event_name, "user_id": str(UID), "registration_timestamp": NOW.isoformat()},
        separators=(",", ":"),
    ).encode("utf-8")
    raw = base + b" " * (4097 - len(base))
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post("/registrations/remove", data=raw, headers=HEADERS)
        await reply.read()
    assert reply.status == 413
    client.get_user.assert_not_called()


@pytest.mark.parametrize(
    "body_change",
    [
        {"unexpected": True},
        {"registration_timestamp": "not-a-timestamp"},
        {"registration_timestamp": "2026-10-01T12:00:00"},
        {"changes": {}},
        {"changes": {"display_name": "   "}},
        {"changes": {"display_name": "x" * 33}},
        {"changes": {"extras_names": [{"index": 6, "value": "Out of range"}]}},
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
    client.get_user.assert_not_called()
    client.fetch_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()


async def test_authentication_and_event_guards(names_setup, monkeypatch):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)

    request = request_body(event, body)
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        assert (await http.post("/registrations/names", json=request, headers={})).status == 401
        assert (
            await http.post("/registrations/names", json=request, headers={"Authorization": "Bearer wrong"})
        ).status == 401

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
    assert result["warnings"] == []
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
    client.get_user.assert_not_called()
    client.fetch_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()
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


async def test_unchanged_supplied_field_is_omitted_from_dm(names_setup):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    user = client.get_user.return_value
    sent = []

    async def send(message, **kwargs):
        sent.append(message)

    user.send.side_effect = send
    status, result = await post_names(
        client,
        event,
        payload(
            event,
            current,
            changes={
                "display_name": "Before",
                "extras_names": [{"index": 0, "value": "Alicia"}],
            },
        ),
    )
    assert status == 200
    assert result["saved"] is True
    assert len(sent) == 1
    assert "👥 Guest 1: ~~Alice~~ → **Alicia**" in sent[0]
    assert "👥 同伴者1：~~Alice~~ → **Alicia**" in sent[0]
    assert "👤 Name:" not in sent[0]
    assert "👤 名前：" not in sent[0]


@pytest.mark.parametrize("failure_stage", ["fetch", "send"])
async def test_http_notification_failure_keeps_saved_names_and_returns_one_warning(names_setup, failure_stage):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    user = client.get_user.return_value
    if failure_stage == "fetch":
        client.get_user.return_value = None
        client.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Missing user"))
    else:
        user.send.side_effect = discord.Forbidden(MagicMock(status=403), "DM closed")

    status, result = await post_names(client, event, payload(event, current))

    assert status == 200
    assert result["saved"] is True
    assert result["warnings"] == [admin_api.NAME_CHANGE_NOTIFICATION_WARNING]
    assert result["registration"]["display_name"] == "After"
    assert get_responses(event.event_name)[0].display_name == "After"
    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk[event.event_name]["attendees"][0]["display_name"] == "After"


async def test_postcommit_formatting_failure_is_a_warning_not_a_failed_save(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    with patch(
        "offkai_bot.admin_api.build_attendee_names_changed_message",
        side_effect=ValueError("synthetic formatting failure"),
    ):
        status, result = await post_names(client, event, payload(event, current))

    assert status == 200
    assert result["saved"] is True
    assert result["warnings"] == [admin_api.NAME_CHANGE_NOTIFICATION_WARNING]
    assert get_responses(event.event_name)[0].display_name == "After"
    assert json.loads(path.read_text(encoding="utf-8"))[event.event_name]["attendees"][0]["display_name"] == "After"
    client.get_user.return_value.send.assert_not_awaited()


async def test_no_actual_persisted_change_skips_dm_and_reports_saved(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    status, result = await post_names(
        client,
        event,
        payload(
            event,
            current,
            changes={
                "display_name": "  Before  ",
                "extras_names": [{"index": 0, "value": "Alice"}],
            },
        ),
    )
    assert status == 200
    assert result["saved"] is True
    assert result["warnings"] == []
    assert path.read_bytes() == disk
    client.get_user.assert_not_called()
    client.fetch_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()


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
    client.get_user.assert_not_called()
    client.fetch_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()


@pytest.mark.parametrize(
    "value",
    [
        "\u0000name",
        "name\u0001",
        "\u007fname",
        "na\u0085me",
        "\ud800name",
        "name\udfff",
    ],
)
async def test_edited_names_reject_control_characters_and_surrogates_before_trimming(names_setup, value):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    body = payload(event, current, changes={"display_name": f"  {value}  "})
    status, result = await post_names(client, event, body)
    assert status == 400
    assert result == {"error": "invalid_request", "saved": False}
    assert get_responses(event.event_name)[0].display_name == "Before"
    assert path.read_bytes() == disk
    client.get_user.return_value.send.assert_not_awaited()


async def test_edited_control_character_is_rejected_in_guest_names_too(names_setup):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current, changes={"extras_names": [{"index": 0, "value": "Ali\u0000ce"}]})
    status, result = await post_names(client, event, body)
    assert status == 400
    assert result == {"error": "invalid_request", "saved": False}
    assert get_responses(event.event_name)[0].extras_names == ["Alice", "Bob"]
