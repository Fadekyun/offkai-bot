"""Focused coverage for the admin attendee/guest name mutation."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from offkai_bot.data import response as response_data
from offkai_bot.data.response import (
    EventData,
    Response,
    WaitlistEntry,
    build_attendee_report_rows,
    calculate_attendance,
    update_attendee_names,
)
from offkai_bot.errors import RegistrationChangedError

NOW = datetime(2026, 10, 1, 12, 0, 0, 123000, tzinfo=UTC)
UID = 101000000000000001


def attendee(**overrides) -> Response:
    values = {
        "user_id": UID,
        "username": "discord-name",
        "extra_people": 2,
        "behavior_confirmed": True,
        "arrival_confirmed": True,
        "event_name": "Names",
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


def write_cache(tmp_path, monkeypatch, records: list[Response], waitlist=None) -> Path:
    path = tmp_path / "responses.json"
    monkeypatch.setattr(response_data, "get_config", lambda: {"RESPONSES_FILE": str(path)})
    response_data.RESPONSE_DATA_CACHE = {
        "Names": EventData(attendees=records, waitlist=waitlist or []),
        "Other": EventData(attendees=[], waitlist=[]),
    }
    response_data.save_responses()
    return path


def edit(**overrides):
    current = response_data.get_responses("Names")[0]
    arguments = {
        "expected_timestamp": NOW,
        "expected_display_name": current.display_name,
        "expected_extra_people": current.extra_people,
        "expected_extras_names": [
            current.extras_names[index] if index < len(current.extras_names) else None
            for index in range(current.extra_people)
        ]
        + list(current.extras_names[current.extra_people :]),
    }
    arguments.update(overrides)
    return update_attendee_names("Names", UID, **arguments)


def test_roundtrip_updates_only_names_in_cache_and_on_disk(tmp_path, monkeypatch):
    original = attendee()
    path = write_cache(tmp_path, monkeypatch, [original])
    untouched = replace(original)
    untouched.extras_names = list(original.extras_names)

    updated = edit(display_name="  After  ", guest_names=[(0, "  Alicia  "), (1, "Bobby")])

    assert updated is not original
    assert updated.display_name == "After"
    assert updated.extras_names == ["Alicia", "Bobby"]
    assert original.display_name == "Before"
    assert original.extras_names == ["Alice", "Bob"]
    assert updated.extras_names is not original.extras_names
    for field in (
        "user_id",
        "username",
        "extra_people",
        "behavior_confirmed",
        "arrival_confirmed",
        "event_name",
        "timestamp",
        "drinks",
        "payment_method",
        "attendee_number",
        "extras_attendee_numbers",
    ):
        assert getattr(updated, field) == getattr(untouched, field)

    disk = json.loads(path.read_text(encoding="utf-8"))
    saved = disk["Names"]["attendees"][0]
    assert saved["display_name"] == "After"
    assert saved["extras_names"] == ["Alicia", "Bobby"]
    assert saved["payment_method"] == "Bank"
    assert saved["attendee_number"] == 4
    assert disk["Other"] == {"attendees": [], "waitlist": []}

    response_data.RESPONSE_DATA_CACHE = None
    reloaded = response_data.get_responses("Names")[0]
    assert reloaded.display_name == "After"
    assert reloaded.extras_names == ["Alicia", "Bobby"]
    assert reloaded.payment_method == "Bank"

    total, names = calculate_attendance("Names", nicknames=True)
    assert total == 3
    assert any("After (@discord-name)" in name for name in names)
    rows = build_attendee_report_rows("Names")
    assert [row.name for row in rows] == ["After", "Alicia", "Bobby"]


def test_zero_guests_only_edits_display_name(tmp_path, monkeypatch):
    original = attendee(extra_people=0, extras_names=[], drinks=["Tea"], extras_attendee_numbers=[])
    write_cache(tmp_path, monkeypatch, [original])

    updated = edit(display_name="No Guests", guest_names=[])

    assert updated.display_name == "No Guests"
    assert updated.extras_names == []
    assert updated.extra_people == 0


@pytest.mark.parametrize("guest_count", range(6))
def test_every_supported_guest_count_edits_only_the_last_existing_slot(tmp_path, monkeypatch, guest_count):
    original_names = [f"Guest {index + 1}" for index in range(guest_count)]
    guest_numbers = list(range(5, 5 + guest_count))
    original = attendee(
        extra_people=guest_count,
        extras_names=original_names,
        extras_attendee_numbers=guest_numbers,
        drinks=[f"Drink {index + 1}" for index in range(guest_count + 1)],
    )
    write_cache(tmp_path, monkeypatch, [original])

    if guest_count == 0:
        updated = edit(display_name="Display Only")
        assert updated.display_name == "Display Only"
        assert updated.extras_names == []
    else:
        updated = edit(guest_names=[(guest_count - 1, "  Updated Last  ")])
        assert updated.display_name == "Before"
        assert updated.extras_names == [*original_names[:-1], "Updated Last"]
        assert updated.extras_names[: guest_count - 1] == original_names[:-1]

    assert updated.user_id == original.user_id
    assert updated.username == original.username
    assert updated.timestamp == original.timestamp
    assert updated.extra_people == guest_count
    assert updated.attendee_number == original.attendee_number
    assert updated.extras_attendee_numbers == guest_numbers
    assert updated.drinks == original.drinks

    response_data.RESPONSE_DATA_CACHE = None
    reloaded = response_data.get_responses("Names")[0]
    assert reloaded.extra_people == guest_count
    assert reloaded.extras_names == updated.extras_names
    assert reloaded.extras_attendee_numbers == guest_numbers


def test_legacy_missing_display_name_guest_only_edit_matches_loaded_null(tmp_path, monkeypatch):
    path = tmp_path / "responses.json"
    raw_record = asdict(attendee(extra_people=1, extras_names=["Alice"], extras_attendee_numbers=[5]))
    raw_record.pop("display_name")
    raw_record["timestamp"] = NOW.isoformat()
    assert "display_name" not in raw_record
    path.write_text(json.dumps({"Names": {"attendees": [raw_record], "waitlist": []}}), encoding="utf-8")
    monkeypatch.setattr(response_data, "get_config", lambda: {"RESPONSES_FILE": str(path)})
    response_data.RESPONSE_DATA_CACHE = None

    loaded = response_data.get_responses("Names")[0]
    assert loaded.display_name is None
    updated = update_attendee_names(
        "Names",
        UID,
        NOW,
        None,
        1,
        ["Alice"],
        guest_names=[(0, "  Alicia  ")],
    )
    assert updated.display_name is None
    assert updated.extras_names == ["Alicia"]

    response_data.RESPONSE_DATA_CACHE = None
    reloaded = response_data.get_responses("Names")[0]
    assert reloaded.display_name is None
    assert reloaded.extras_names == ["Alicia"]


def test_sparse_legacy_slots_are_preserved_or_materialised_only_when_needed(tmp_path, monkeypatch):
    original = attendee(extra_people=3, extras_names=["", "Bob"])
    write_cache(tmp_path, monkeypatch, [original])

    display_only = edit(display_name="Sparse")
    assert display_only.extras_names == ["", "Bob"]

    slot_two = edit(display_name="Sparse", guest_names=[(2, "Carol")])
    assert slot_two.extras_names == ["", "Bob", "Carol"]

    original = attendee(extra_people=2, extras_names=["Alice", "Bob"])
    response_data.RESPONSE_DATA_CACHE["Names"] = EventData(attendees=[original], waitlist=[])
    slot_one = update_attendee_names(
        "Names",
        UID,
        NOW,
        "Before",
        2,
        ["Alice", "Bob"],
        guest_names=[(1, "  Bea  ")],
    )
    assert slot_one.extras_names == ["Alice", "Bea"]


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"display_name": "   "}, "cannot be blank"),
        ({"display_name": "x" * 33}, "at most 32"),
        ({"guest_names": [(0, "   ")]}, "cannot be blank"),
        ({"guest_names": [(0, "x" * 161)]}, "at most 160"),
        ({"guest_names": [(3, "Out of range")]}, "out of range"),
        ({"guest_names": [(0, "One"), (0, "Again")]}, "more than once"),
        ({"display_name": "X", "expected_extra_people": 1, "expected_extras_names": ["Alice"]}, "changed"),
    ],
)
def test_invalid_or_stale_edits_leave_cache_and_disk_unchanged(tmp_path, monkeypatch, overrides, message):
    original = attendee()
    path = write_cache(tmp_path, monkeypatch, [original])
    before_cache = response_data.RESPONSE_DATA_CACHE
    before_disk = path.read_bytes()
    with pytest.raises((ValueError, RegistrationChangedError), match=message):
        edit(**overrides)
    assert response_data.RESPONSE_DATA_CACHE is before_cache
    assert response_data.RESPONSE_DATA_CACHE["Names"]["attendees"][0] is original
    assert path.read_bytes() == before_disk


def test_surplus_legacy_names_are_part_of_the_conflict_snapshot_and_are_preserved(tmp_path, monkeypatch):
    original = attendee(extra_people=1, extras_names=["Alice", "Legacy surplus"], extras_attendee_numbers=[5])
    write_cache(tmp_path, monkeypatch, [original])

    with pytest.raises(RegistrationChangedError):
        edit(display_name="Stale", expected_extras_names=["Alice", "Changed surplus"])
    assert response_data.get_responses("Names")[0].display_name == "Before"

    updated = edit(display_name="After", guest_names=[(0, "Alicia")])
    assert updated.extras_names == ["Alicia", "Legacy surplus"]


@pytest.mark.parametrize(
    "mutation",
    ["rejoin", "display_name", "guest_count", "guest_slot", "demoted"],
)
def test_stale_snapshot_is_rejected(tmp_path, monkeypatch, mutation):
    original = attendee()
    waitlist = []
    records = [original]
    write_cache(tmp_path, monkeypatch, records, waitlist)
    expected_display = original.display_name
    expected_extra = original.extra_people
    expected_names = ["Alice", "Bob"]

    expected_timestamp = NOW - timedelta(minutes=1) if mutation == "rejoin" else NOW
    if mutation == "display_name":
        original.display_name = "Changed elsewhere"
    elif mutation == "guest_count":
        original.extra_people = 1
        original.extras_names = ["Alice"]
    elif mutation == "guest_slot":
        original.extras_names = ["Alice", "Changed"]
    elif mutation == "demoted":
        waitlist_fields = WaitlistEntry.__dataclass_fields__
        response_data.RESPONSE_DATA_CACHE["Names"] = EventData(
            attendees=[],
            waitlist=[
                WaitlistEntry(**{key: value for key, value in asdict(original).items() if key in waitlist_fields})
            ],
        )

    with pytest.raises(RegistrationChangedError):
        update_attendee_names(
            "Names",
            UID,
            expected_timestamp,
            expected_display,
            expected_extra,
            expected_names,
            display_name="Stale",
        )
    assert response_data.RESPONSE_DATA_CACHE["Names"]["attendees"] == ([] if mutation == "demoted" else [original])


def test_two_admin_edits_conflict_and_second_does_not_overwrite_first(tmp_path, monkeypatch):
    original = attendee()
    write_cache(tmp_path, monkeypatch, [original])
    stale_snapshot = {
        "expected_display_name": "Before",
        "expected_extra_people": 2,
        "expected_extras_names": ["Alice", "Bob"],
    }

    first = update_attendee_names(
        "Names", UID, NOW, "Before", 2, ["Alice", "Bob"], display_name="First", guest_names=[(0, "Alicia")]
    )
    assert first.display_name == "First"
    with pytest.raises(RegistrationChangedError):
        update_attendee_names("Names", UID, NOW, display_name="Second", **stale_snapshot)
    assert response_data.get_responses("Names")[0].display_name == "First"
    assert response_data.get_responses("Names")[0].extras_names == ["Alicia", "Bob"]


def test_unrelated_bot_updates_are_preserved_and_survive_later_bot_save(tmp_path, monkeypatch):
    original = attendee()
    write_cache(tmp_path, monkeypatch, [original])
    original.payment_method = "PayNow"
    original.payment_selection_expires_at = NOW + timedelta(hours=2)
    original.behavior_confirmed = False
    response_data.save_responses()

    updated = update_attendee_names("Names", UID, NOW, "Before", 2, ["Alice", "Bob"], display_name="After")
    assert updated.payment_method == "PayNow"
    assert updated.payment_selection_expires_at == NOW + timedelta(hours=2)
    assert updated.behavior_confirmed is False

    # A later bot save and cache reload keep the corrected names.
    updated.arrival_confirmed = False
    response_data.save_responses()
    response_data.RESPONSE_DATA_CACHE = None
    reloaded = response_data.get_responses("Names")[0]
    assert reloaded.display_name == "After"
    assert reloaded.payment_method == "PayNow"
    assert reloaded.arrival_confirmed is False


def test_other_attendees_and_later_bot_actions_are_preserved(tmp_path, monkeypatch):
    original = attendee()
    other = attendee(
        user_id=UID + 1,
        username="other",
        display_name="Other",
        extra_people=0,
        extras_names=[],
        drinks=[],
        attendee_number=7,
        extras_attendee_numbers=[],
    )
    write_cache(tmp_path, monkeypatch, [original, other])

    update_attendee_names("Names", UID, NOW, "Before", 2, ["Alice", "Bob"], display_name="After")
    records = response_data.get_responses("Names")
    assert len(records) == 2
    assert records[0].display_name == "After"
    assert records[1] is other

    # Normal bot removals still work and keep the corrected names on disk.
    response_data.remove_response("Names", UID + 1)
    assert [record.user_id for record in response_data.get_responses("Names")] == [UID]
    assert response_data.get_responses("Names")[0].display_name == "After"


def test_failed_atomic_write_leaves_cache_and_disk_unchanged(tmp_path, monkeypatch):
    original = attendee()
    path = write_cache(tmp_path, monkeypatch, [original])
    cache = response_data.RESPONSE_DATA_CACHE
    disk = path.read_bytes()
    with (
        patch("offkai_bot.data.response.atomic_write_json", side_effect=OSError("disk unavailable")),
        pytest.raises(OSError, match="disk unavailable"),
    ):
        update_attendee_names("Names", UID, NOW, "Before", 2, ["Alice", "Bob"], display_name="After")
    assert response_data.RESPONSE_DATA_CACHE is cache
    assert response_data.get_responses("Names")[0] is original
    assert original.display_name == "Before"
    assert original.extras_names == ["Alice", "Bob"]
    assert path.read_bytes() == disk
    assert not list(path.parent.glob("*.tmp"))


def test_millisecond_identity_match_accepts_submillisecond_storage(tmp_path, monkeypatch):
    original = attendee(timestamp=NOW.replace(microsecond=123456))
    write_cache(tmp_path, monkeypatch, [original])
    expected = NOW
    updated = update_attendee_names("Names", UID, expected, "Before", 2, ["Alice", "Bob"], display_name="After")
    assert updated.display_name == "After"
