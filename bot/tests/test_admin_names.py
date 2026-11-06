"""Authenticated admin name-edit endpoint and its conflict/security boundaries."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from offkai_bot import admin_api
from offkai_bot.data import response as response_data
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


def stage_body(event: Event, body: dict, field: str, value: str, *, manifest, batch_id: str | None = None):
    staged = {
        "operation": "stage",
        "event_name": event.event_name,
        "user_id": body["user_id"],
        "registration_timestamp": body["registration_timestamp"],
        "expected_revision": body["expected_revision"],
        "manifest": manifest,
        "field": field,
        "value": value,
    }
    if batch_id is not None:
        staged["batch_id"] = batch_id
    return staged


def commit_body(event: Event, body: dict, manifest, batch_id: str):
    return {
        "operation": "commit",
        "event_name": event.event_name,
        "user_id": body["user_id"],
        "registration_timestamp": body["registration_timestamp"],
        "expected_revision": body["expected_revision"],
        "manifest": manifest,
        "batch_id": batch_id,
    }


async def save_names(http, event: Event, body: dict, *, sizes: list[int] | None = None):
    allowed = {"event_name", "user_id", "registration_timestamp", "expected_revision", "changes"}
    if set(body) - allowed:
        staged = stage_body(event, body, "display_name", "After", manifest=["display_name"])
        staged.update({key: value for key, value in body.items() if key not in allowed})
        raw = json.dumps(staged, separators=(",", ":")).encode("utf-8")
        if sizes is not None:
            sizes.append(len(raw))
        reply = await http.post("/registrations/names", data=raw, headers=HEADERS)
        return reply.status, await reply.json()
    changes = body.get("changes")
    parts = name_parts(changes)
    manifest = [field for field, _value in parts]
    if not parts or len(set(manifest)) != len(manifest) or set(changes or {}) - {"display_name", "extras_names"}:
        # Send a harmless-shaped invalid stage so the server still owns rejection.
        raw = json.dumps(stage_body(event, body, "unknown", "x", manifest=["unknown"]), separators=(",", ":")).encode(
            "utf-8"
        )
        if sizes is not None:
            sizes.append(len(raw))
        reply = await http.post("/registrations/names", data=raw, headers=HEADERS)
        return reply.status, await reply.json()
    batch_id = None
    for field, value in parts:
        raw = json.dumps(
            stage_body(event, body, field, value, manifest=manifest, batch_id=batch_id),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if sizes is not None:
            sizes.append(len(raw))
        reply = await http.post("/registrations/names", data=raw, headers=HEADERS)
        result = await reply.json()
        if reply.status != 200:
            return reply.status, result
        batch_id = result["batch_id"]
    assert batch_id is not None
    raw = json.dumps(commit_body(event, body, manifest, batch_id), separators=(",", ":")).encode("utf-8")
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
async def test_max_length_multibyte_names_use_bounded_parts_and_one_commit(names_setup, guest_count):
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
    assert len(sizes) == guest_count + 2  # part per field plus one commit
    assert all(size <= 4096 for size in sizes), sizes
    saved = get_responses(event.event_name)[0]
    assert saved.display_name == new_display
    assert saved.extras_names == new_guests
    assert json.loads(path.read_text(encoding="utf-8"))[event.event_name]["attendees"][0]["display_name"] == new_display
    assert client.get_user.return_value.send.await_count == 1


async def test_exact_4k_stage_request_is_accepted_and_one_byte_over_is_rejected(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current, changes={"display_name": "After"})
    base = json.dumps(
        stage_body(event, body, "display_name", "After", manifest=["display_name"]),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    exact = base + b" " * (4096 - len(base))
    assert len(exact) == 4096

    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post("/registrations/names", data=exact, headers=HEADERS)
        staged = await reply.json()
        assert reply.status == 200, staged
        reply = await http.post(
            "/registrations/names",
            json=commit_body(event, body, ["display_name"], staged["batch_id"]),
            headers=HEADERS,
        )
        result = await reply.json()
    assert reply.status == 200, result
    assert result["saved"] is True
    assert get_responses(event.event_name)[0].display_name == "After"
    assert client.get_user.return_value.send.await_count == 1

    # Fresh registration for the rejection case.
    response_data.RESPONSE_DATA_CACHE = None
    path.write_text("{}")
    load_responses()
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    too_large = exact + b" "
    assert len(too_large) == 4097
    status, result = await post_names_raw(client, too_large)
    assert status == 413
    assert result == {"error": "request_too_large", "saved": False}
    assert get_responses(event.event_name)[0].display_name == "Before"
    assert path.read_bytes() == disk
    assert client.get_user.return_value.send.await_count == 1  # only the accepted first request
    assert client.get_user.call_count == 1


async def test_streamed_body_over_4k_is_rejected_without_usable_content_length(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    body = payload(event, current, changes={"display_name": "After"})
    base = json.dumps(
        stage_body(event, body, "display_name", "After", manifest=["display_name"]),
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
    client.get_user.assert_not_called()
    client.fetch_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()


async def test_authentication_and_event_guards(names_setup, monkeypatch):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)

    staged = stage_body(event, body, "display_name", "After", manifest=["display_name"])
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        assert (await http.post("/registrations/names", json=staged, headers={})).status == 401
        assert (
            await http.post("/registrations/names", json=staged, headers={"Authorization": "Bearer wrong"})
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


async def test_committed_event_name_is_used_if_the_event_mutates_during_dm_prefetch(names_setup):
    event, client, _ = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)
    user = client.get_user.return_value
    sent = []

    async def send(message, **kwargs):
        sent.append(message)

    async def fetch_user(_user_id):
        event.event_name = "Renamed after commit"
        return user

    user.send.side_effect = send
    client.get_user.return_value = None
    client.fetch_user = AsyncMock(side_effect=fetch_user)
    status, result = await post_names(client, event, body)

    assert status == 200
    assert result["warnings"] == []
    assert len(sent) == 1
    assert "**Names endpoint**" in sent[0]
    assert "Renamed after commit" not in sent[0]
    assert get_responses("Names endpoint")[0].display_name == "After"


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


async def test_staging_leaves_cache_disk_and_dm_untouched(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    cache = get_responses(event.event_name)
    body = payload(
        event,
        current,
        changes={
            "display_name": "After",
            "extras_names": [{"index": 0, "value": "Alicia"}],
        },
    )
    manifest = ["display_name", "guest:0"]
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=manifest),
            headers=HEADERS,
        )
        staged = await reply.json()
    assert reply.status == 200
    assert staged["complete"] is False
    assert get_responses(event.event_name) is cache
    assert get_responses(event.event_name)[0] is current
    assert path.read_bytes() == disk
    client.get_user.assert_not_called()
    client.get_user.return_value.send.assert_not_awaited()


async def test_incomplete_batch_never_partially_writes_and_can_still_complete(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    body = payload(
        event,
        current,
        changes={
            "display_name": "After",
            "extras_names": [{"index": 0, "value": "Alicia"}],
        },
    )
    manifest = ["display_name", "guest:0"]
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=manifest),
            headers=HEADERS,
        )
        batch_id = (await reply.json())["batch_id"]
        reply = await http.post(
            "/registrations/names",
            json=commit_body(event, body, manifest, batch_id),
            headers=HEADERS,
        )
        result = await reply.json()
        assert reply.status == 409
        assert result == {"error": "batch_incomplete", "saved": False}
        assert get_responses(event.event_name)[0].display_name == "Before"
        assert path.read_bytes() == disk
        assert client.get_user.return_value.send.await_count == 0

        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "guest:0", "Alicia", manifest=manifest, batch_id=batch_id),
            headers=HEADERS,
        )
        assert reply.status == 200
        reply = await http.post(
            "/registrations/names",
            json=commit_body(event, body, manifest, batch_id),
            headers=HEADERS,
        )
        result = await reply.json()
    assert reply.status == 200, result
    assert result["saved"] is True
    assert get_responses(event.event_name)[0].extras_names == ["Alicia", "Bob"]
    assert client.get_user.return_value.send.await_count == 1


async def test_expired_batch_is_rejected_without_mutation_or_dm(names_setup, monkeypatch):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    disk = path.read_bytes()
    body = payload(event, current)
    clock = [1000.0]
    monkeypatch.setattr(admin_api, "_name_batch_now", lambda: clock[0])
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=["display_name"]),
            headers=HEADERS,
        )
        batch_id = (await reply.json())["batch_id"]
        clock[0] += admin_api.NAME_BATCH_TTL_SECONDS + 1
        reply = await http.post(
            "/registrations/names",
            json=commit_body(event, body, ["display_name"], batch_id),
            headers=HEADERS,
        )
        result = await reply.json()
    assert reply.status == 409
    assert result == {"error": "batch_expired", "saved": False}
    assert path.read_bytes() == disk
    assert get_responses(event.event_name)[0].display_name == "Before"
    client.get_user.return_value.send.assert_not_awaited()


async def test_active_batch_count_is_bounded(names_setup):
    event, client, _ = names_setup
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        for index in range(admin_api.MAX_ACTIVE_NAME_BATCHES):
            body = payload(event, make_attendee(), user_id=str(UID + index))
            reply = await http.post(
                "/registrations/names",
                json=stage_body(event, body, "display_name", "After", manifest=["display_name"]),
                headers=HEADERS,
            )
            assert reply.status == 200
        body = payload(event, make_attendee(), user_id=str(UID + admin_api.MAX_ACTIVE_NAME_BATCHES))
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=["display_name"]),
            headers=HEADERS,
        )
        result = await reply.json()
    assert reply.status == 409
    assert result == {"error": "batch_busy", "saved": False}
    client.get_user.return_value.send.assert_not_awaited()


async def test_stage_context_mismatch_and_conflicting_replay_are_rejected(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(
        event,
        current,
        changes={
            "display_name": "After",
            "extras_names": [{"index": 0, "value": "Alicia"}],
        },
    )
    manifest = ["display_name", "guest:0"]
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=manifest),
            headers=HEADERS,
        )
        batch_id = (await reply.json())["batch_id"]

        wrong_context = stage_body(event, body, "guest:0", "Alicia", manifest=manifest, batch_id=batch_id)
        wrong_context["registration_timestamp"] = (NOW + timedelta(seconds=1)).isoformat()
        reply = await http.post("/registrations/names", json=wrong_context, headers=HEADERS)
        assert reply.status == 409
        assert await reply.json() == {"error": "batch_context_mismatch", "saved": False}

        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "Different", manifest=manifest, batch_id=batch_id),
            headers=HEADERS,
        )
        assert reply.status == 409
        assert await reply.json() == {"error": "batch_conflict", "saved": False}

        # Same-value replay is harmless and the untouched manifest can finish.
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=manifest, batch_id=batch_id),
            headers=HEADERS,
        )
        assert reply.status == 200
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "guest:0", "Alicia", manifest=manifest, batch_id=batch_id),
            headers=HEADERS,
        )
        assert reply.status == 200
    assert get_responses(event.event_name)[0].display_name == "Before"


async def test_replayed_commit_returns_unknown_without_a_second_dm(names_setup):
    event, client, path = names_setup
    current = make_attendee()
    add_response(event.event_name, current)
    body = payload(event, current)
    commit = None
    async with TestClient(TestServer(admin_api.create_admin_app(client))) as http:
        reply = await http.post(
            "/registrations/names",
            json=stage_body(event, body, "display_name", "After", manifest=["display_name"]),
            headers=HEADERS,
        )
        batch_id = (await reply.json())["batch_id"]
        commit = commit_body(event, body, ["display_name"], batch_id)
        reply = await http.post("/registrations/names", json=commit, headers=HEADERS)
        assert reply.status == 200
        assert client.get_user.return_value.send.await_count == 1
        saved_disk = path.read_bytes()

        reply = await http.post("/registrations/names", json=commit, headers=HEADERS)
        result = await reply.json()
    assert reply.status == 409
    assert result == {"error": "batch_unknown", "unconfirmed": True}
    assert "saved" not in result
    assert path.read_bytes() == saved_disk
    assert client.get_user.return_value.send.await_count == 1
