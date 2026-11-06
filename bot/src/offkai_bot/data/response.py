# src/offkai_bot/data/response.py
import json
import logging
import os
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

# Use relative imports for sibling modules within the package
from offkai_bot.config import get_config
from offkai_bot.data.atomic import atomic_write_json, backup_corrupted_file
from offkai_bot.data.encoders import DataclassJSONEncoder
from offkai_bot.errors import (
    DuplicateResponseError,
    NoResponsesFoundError,
    NoWaitlistEntriesFoundError,
    RegistrationChangedError,
    ResponseNotFoundError,
)

if TYPE_CHECKING:
    from offkai_bot.data.event import Event

_log = logging.getLogger(__name__)


# --- Response Dataclass ---
@dataclass
class Response:
    user_id: int
    username: str
    extra_people: int
    behavior_confirmed: bool
    arrival_confirmed: bool
    event_name: str
    timestamp: datetime
    drinks: list[str] = field(default_factory=list)
    extras_names: list[str] = field(default_factory=list)
    payment_method: str | None = None
    no_show_agreed: bool = False
    display_name: str | None = None
    attendee_number: int | None = None
    extras_attendee_numbers: list[int] = field(default_factory=list)
    payment_selection_expires_at: datetime | None = None
    payment_selection_demotes: bool = False


# --- Waitlist Entry Dataclass ---
@dataclass
class WaitlistEntry:
    user_id: int
    username: str
    extra_people: int
    behavior_confirmed: bool
    arrival_confirmed: bool
    event_name: str
    timestamp: datetime
    drinks: list[str] = field(default_factory=list)
    extras_names: list[str] = field(default_factory=list)
    payment_method: str | None = None
    no_show_agreed: bool = False
    display_name: str | None = None
    payment_selection_demotes: bool = False


@dataclass(frozen=True)
class AttendeeNameChange:
    """One persisted name field change captured during the atomic commit."""

    guest_number: int | None  # None for the primary attendee, 1-based for guests
    old_name: str | None
    new_name: str


@dataclass(frozen=True)
class AttendeeNameUpdate:
    """The committed record plus immutable old/new values for notifications."""

    registration: Response
    changes: tuple[AttendeeNameChange, ...]


def get_effective_display_name(entry: Response | WaitlistEntry) -> str:
    """Return the event snapshot name, falling back safely for legacy records."""

    if isinstance(entry.display_name, str):
        display_name = entry.display_name.strip()
        if display_name:
            return display_name
    return entry.username


# Admin name edit bounds. The display-name limit mirrors the signup modal's
# 32-character preferred-name field. The 160-character guest-name limit is an
# explicit admin-edit bound derived from the custom signup form's combined
# extras-names field; the default signup form had no separate per-name cap.
# Neither bound is applied to untouched legacy values: only edited values are
# trimmed and validated, and untouched names are never truncated.
MAX_DISPLAY_NAME_LENGTH = 32
MAX_GUEST_NAME_LENGTH = 160
MAX_EXTRA_PEOPLE = 5


def _normalized_edited_name(value: object, max_length: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    name = value.strip()
    if not name:
        raise ValueError(f"{field} cannot be blank")
    if len(name) > max_length:
        raise ValueError(f"{field} must be at most {max_length} characters")
    return name


def format_organizer_name(entry: Response | WaitlistEntry, *, nicknames: bool = False) -> str:
    """Format an entry for organizer-facing attendance and waitlist output."""

    effective_name = get_effective_display_name(entry)
    if not nicknames or effective_name == entry.username:
        return effective_name
    return f"{effective_name} (@{entry.username})"


@dataclass(frozen=True)
class AttendeeReportRow:
    attendee_number: int
    name: str
    type: str
    registered_by_username: str
    registered_by_display_name: str
    registered_by_user_id: int
    guest_index: int | None
    drink: str


# --- Type Definitions ---
class PaymentExpiryBackfill(TypedDict):
    target_count: int
    eligible_registrations: list[str]
    excluded_user_ids: list[int]


class EventData(TypedDict):
    attendees: list[Response]
    waitlist: list[WaitlistEntry]
    payment_expiry_backfill: NotRequired[PaymentExpiryBackfill | None]


def registration_key(entry: Response | WaitlistEntry) -> str:
    return f"{entry.user_id}:{entry.timestamp.isoformat()}"


def _parse_payment_expiry_backfill(value: Any) -> PaymentExpiryBackfill | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("target_count")) is not int
        or value["target_count"] < 0
        or not isinstance(value.get("eligible_registrations"), list)
        or not all(isinstance(key, str) for key in value["eligible_registrations"])
        or not isinstance(value.get("excluded_user_ids"), list)
        or not all(type(user_id) is int for user_id in value["excluded_user_ids"])
    ):
        _log.warning("Invalid payment expiry backfill metadata; preserving the roster without this request")
        return None
    return PaymentExpiryBackfill(
        target_count=value["target_count"],
        eligible_registrations=value["eligible_registrations"],
        excluded_user_ids=value["excluded_user_ids"],
    )


class NumberedAttendeeName(str):
    attendee_number: int | None

    def __new__(cls, value: str, attendee_number: int | None = None):
        obj = str.__new__(cls, value)
        obj.attendee_number = attendee_number
        return obj


# --- Response Data Handling ---

RESPONSE_DATA_CACHE: dict[str, EventData] | None = None


def _parse_optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _parse_required_int(value)


def _parse_int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [_parse_required_int(item) for item in value]


def _parse_required_int(value: object) -> int:
    if not isinstance(value, int | str):
        raise TypeError(f"Expected int-compatible attendee number, got {type(value).__name__}")
    return int(value)


def _group_attendee_numbers(response: Response) -> list[int]:
    numbers = []
    if response.attendee_number is not None:
        numbers.append(response.attendee_number)
    numbers.extend(response.extras_attendee_numbers)
    return numbers


def _event_has_attendee_numbers(event_data: EventData) -> bool:
    return any(_group_attendee_numbers(response) for response in event_data["attendees"])


def _next_attendee_number(event_data: EventData) -> int:
    assigned_numbers = [number for response in event_data["attendees"] for number in _group_attendee_numbers(response)]
    return max(assigned_numbers, default=0) + 1


def _max_attendee_number(event_data: EventData) -> int:
    assigned_numbers = [number for response in event_data["attendees"] for number in _group_attendee_numbers(response)]
    return max(assigned_numbers, default=0)


def _assign_group_numbers(response: Response, start_number: int) -> int:
    response.attendee_number = start_number
    response.extras_attendee_numbers = list(range(start_number + 1, start_number + 1 + response.extra_people))
    return start_number + 1 + response.extra_people


def _clear_group_numbers(response: Response) -> None:
    response.attendee_number = None
    response.extras_attendee_numbers = []


def _number_for_extra(response: Response, index: int) -> int | None:
    if index < len(response.extras_attendee_numbers):
        return response.extras_attendee_numbers[index]
    return None


def _has_complete_attendee_numbers(responses: list[Response]) -> bool:
    expected_count = sum(1 + response.extra_people for response in responses)
    actual_numbers = [number for response in responses for number in _group_attendee_numbers(response)]
    return (
        len(actual_numbers) == expected_count
        and len(set(actual_numbers)) == expected_count
        and all(number > 0 for number in actual_numbers)
    )


def _drink_for(response: Response, index: int) -> str:
    if index < len(response.drinks):
        return response.drinks[index]
    return "N/A"


def _parse_payment_selection_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        expiry = datetime.fromisoformat(value)
        return expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        _log.warning("Invalid payment selection expiry; leaving the registration intact with an expired choice.")
        return datetime.min.replace(tzinfo=UTC)


def _validate_registration_record(record: dict, event_name: str) -> None:
    """Reject malformed persisted fields without removing recoverable registrations."""
    if "user_id" not in record:
        raise ValueError(f"Missing user_id for {event_name}")
    user_id = record["user_id"]
    if isinstance(user_id, bool) or not isinstance(user_id, int | str) or int(user_id) <= 0:
        raise ValueError(f"Invalid user_id for {event_name}")
    for name in ("username", "event_name"):
        if name in record and not isinstance(record[name], str):
            raise ValueError(f"Invalid {name} for {event_name}")
    for name in ("display_name", "payment_method"):
        if record.get(name) is not None and not isinstance(record[name], str):
            raise ValueError(f"Invalid {name} for {event_name}")
    for name in ("drinks", "extras_names"):
        value = record.get(name, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"Invalid {name} for {event_name}")
    extra_people = record.get("extra_people", 0)
    if isinstance(extra_people, bool) or not isinstance(extra_people, int | str) or int(extra_people) < 0:
        raise ValueError(f"Invalid extra_people for {event_name}")
    if record.get("timestamp") not in (None, ""):
        datetime.fromisoformat(record["timestamp"])
    for name in ("behavior_confirmed", "arrival_confirmed"):
        value = record.get(name, False)
        if type(value) is not bool and not (isinstance(value, str) and value.casefold() in ("yes", "no")):
            raise ValueError(f"Invalid {name} for {event_name}")
    if "extras_attendee_numbers" in record and not isinstance(record["extras_attendee_numbers"], list):
        raise ValueError(f"Invalid extras_attendee_numbers for {event_name}")
    for name in ("no_show_agreed", "payment_selection_demotes"):
        if name in record and type(record[name]) is not bool:
            raise ValueError(f"Invalid {name} for {event_name}")
    for number in [record.get("attendee_number"), *record.get("extras_attendee_numbers", [])]:
        if number is not None and (isinstance(number, bool) or not isinstance(number, int | str)):
            raise ValueError(f"Invalid attendee number for {event_name}")


def _parse_response_from_dict(resp_dict: dict, event_name: str) -> Response | None:
    """Parse a Response object from a dictionary."""
    try:
        ts = None
        if "timestamp" in resp_dict and resp_dict["timestamp"]:
            try:
                ts = datetime.fromisoformat(resp_dict["timestamp"])
            except (ValueError, TypeError):
                _log.warning(
                    "Could not parse ISO timestamp '%s' for %s, user %s",
                    resp_dict.get("timestamp"),
                    event_name,
                    resp_dict.get("user_id"),
                )
                ts = None

        drinks = resp_dict.get("drinks", [])
        extra_people = int(resp_dict.get("extra_people", 0))
        behavior_confirmed_raw = resp_dict.get("behavior_confirmed", False)
        arrival_confirmed_raw = resp_dict.get("arrival_confirmed", False)
        behavior_confirmed = str(behavior_confirmed_raw).lower() == "yes" or behavior_confirmed_raw is True
        arrival_confirmed = str(arrival_confirmed_raw).lower() == "yes" or arrival_confirmed_raw is True
        extras_names = resp_dict.get("extras_names", [])
        display_name = resp_dict.get("display_name")
        attendee_number = _parse_optional_int(resp_dict.get("attendee_number"))
        extras_attendee_numbers = _parse_int_list(resp_dict.get("extras_attendee_numbers", []))

        return Response(
            user_id=int(resp_dict.get("user_id", 0)),
            username=resp_dict.get("username", "Unknown User"),
            extra_people=extra_people,
            behavior_confirmed=behavior_confirmed,
            arrival_confirmed=arrival_confirmed,
            event_name=resp_dict.get("event_name", event_name),
            timestamp=(ts if ts is not None else datetime.now(UTC)),
            drinks=drinks,
            extras_names=extras_names,
            display_name=display_name,
            payment_method=resp_dict.get("payment_method"),
            no_show_agreed=resp_dict.get("no_show_agreed", False),
            attendee_number=attendee_number,
            extras_attendee_numbers=extras_attendee_numbers,
            payment_selection_expires_at=_parse_payment_selection_expiry(resp_dict.get("payment_selection_expires_at")),
            payment_selection_demotes=resp_dict.get("payment_selection_demotes") is True,
        )
    except (TypeError, ValueError) as e:
        _log.error("Error creating Response object for event %s from dict %s: %s", event_name, resp_dict, e)
        return None


def _parse_waitlist_entry_from_dict(entry_dict: dict, event_name: str) -> WaitlistEntry | None:
    """Parse a WaitlistEntry object from a dictionary."""
    try:
        ts = None
        if "timestamp" in entry_dict and entry_dict["timestamp"]:
            try:
                ts = datetime.fromisoformat(entry_dict["timestamp"])
            except (ValueError, TypeError):
                _log.warning(
                    "Could not parse ISO timestamp '%s' for %s, user %s",
                    entry_dict.get("timestamp"),
                    event_name,
                    entry_dict.get("user_id"),
                )
                ts = None

        drinks = entry_dict.get("drinks", [])
        extra_people = int(entry_dict.get("extra_people", 0))
        behavior_confirmed_raw = entry_dict.get("behavior_confirmed", False)
        arrival_confirmed_raw = entry_dict.get("arrival_confirmed", False)
        behavior_confirmed = str(behavior_confirmed_raw).lower() == "yes" or behavior_confirmed_raw is True
        arrival_confirmed = str(arrival_confirmed_raw).lower() == "yes" or arrival_confirmed_raw is True
        extras_names = entry_dict.get("extras_names", [])
        display_name = entry_dict.get("display_name")

        return WaitlistEntry(
            user_id=int(entry_dict.get("user_id", 0)),
            username=entry_dict.get("username", "Unknown User"),
            extra_people=extra_people,
            behavior_confirmed=behavior_confirmed,
            arrival_confirmed=arrival_confirmed,
            event_name=entry_dict.get("event_name", event_name),
            timestamp=(ts if ts is not None else datetime.now(UTC)),
            drinks=drinks,
            extras_names=extras_names,
            display_name=display_name,
            payment_selection_demotes=entry_dict.get("payment_selection_demotes") is True,
            payment_method=entry_dict.get("payment_method"),
            no_show_agreed=entry_dict.get("no_show_agreed", False),
        )
    except (TypeError, ValueError) as e:
        _log.error("Error creating WaitlistEntry object for event %s from dict %s: %s", event_name, entry_dict, e)
        return None


def _load_responses() -> dict[str, EventData]:
    """Load current/legacy event records without publishing or rewriting partial data."""
    global RESPONSE_DATA_CACHE
    settings = get_config()
    file_path = settings["RESPONSES_FILE"]
    if not os.path.exists(file_path):
        atomic_write_json(file_path, {}, indent=4)

    def parse_entries(raw: object, event_name: str, *, waitlist: bool) -> list:
        if not isinstance(raw, list):
            raise ValueError(f"Invalid roster list for {event_name}")
        parsed = []
        for record in raw:
            if not isinstance(record, dict):
                raise ValueError(f"Invalid registration record for {event_name}")
            _validate_registration_record(record, event_name)
            entry = (
                _parse_waitlist_entry_from_dict(record, event_name)
                if waitlist
                else _parse_response_from_dict(record, event_name)
            )
            if entry is None:
                raise ValueError(f"Invalid registration record for {event_name}")
            parsed.append(entry)
        return parsed

    try:
        with open(file_path, "r", encoding="utf-8") as file:
            raw_data = json.load(file)
        if not isinstance(raw_data, dict):
            raise ValueError("Responses data must be an object")
        responses_dict: dict[str, EventData] = {}
        legacy_names: set[str] = set()
        # Detect the format per event, not from the first record in the file.
        for event_name, raw in raw_data.items():
            if not event_name:
                raise ValueError("Response event name cannot be empty")
            if isinstance(raw, list):
                legacy_names.add(event_name)
                responses_dict[event_name] = EventData(
                    attendees=parse_entries(raw, event_name, waitlist=False), waitlist=[]
                )
            elif isinstance(raw, dict) and ("attendees" in raw or "waitlist" in raw):
                data = EventData(
                    attendees=parse_entries(raw.get("attendees", []), event_name, waitlist=False),
                    waitlist=parse_entries(raw.get("waitlist", []), event_name, waitlist=True),
                )
                # Invalid optional recovery metadata retains #146's safe roster-only behavior.
                pending = _parse_payment_expiry_backfill(raw.get("payment_expiry_backfill"))
                if pending is not None:
                    data["payment_expiry_backfill"] = pending
                responses_dict[event_name] = data
            else:
                raise ValueError(f"Invalid responses data for {event_name}")

        waitlist_file = settings.get("WAITLIST_FILE")
        if (legacy_names or not raw_data) and waitlist_file and os.path.exists(waitlist_file):
            with open(waitlist_file, "r", encoding="utf-8") as file:
                raw_waitlists = json.load(file)
            if not isinstance(raw_waitlists, dict):
                raise ValueError("Legacy waitlist data must be an object")
            for event_name, raw in raw_waitlists.items():
                entries = parse_entries(raw, event_name, waitlist=True)
                if event_name not in responses_dict:
                    responses_dict[event_name] = EventData(attendees=[], waitlist=entries)
                elif event_name in legacy_names:
                    responses_dict[event_name]["waitlist"] = entries
        # Migration is serialized by the next successful mutation, never during a read.
    except (ValueError, TypeError, OSError):
        backup_corrupted_file(file_path)
        if settings.get("WAITLIST_FILE"):
            backup_corrupted_file(settings["WAITLIST_FILE"])
        _log.exception("Cannot load responses from %s; original data preserved.", file_path)
        raise

    RESPONSE_DATA_CACHE = responses_dict
    return responses_dict


def load_responses() -> dict[str, EventData]:
    """Returns cached response data or loads it if cache is empty."""
    if RESPONSE_DATA_CACHE is not None:
        return RESPONSE_DATA_CACHE
    else:
        return _load_responses()


def save_responses(data: dict[str, EventData] | None = None) -> None:
    """Persist a supplied snapshot or the loaded cache; write failures propagate."""
    if data is None:
        data = RESPONSE_DATA_CACHE
    if data is None:
        raise RuntimeError("Attempted to save response data before loading")
    atomic_write_json(get_config()["RESPONSES_FILE"], data, indent=4, cls=DataclassJSONEncoder, ensure_ascii=False)


def get_responses(event_name: str) -> list[Response]:
    """Gets the list of Response objects (attendees) for a specific event from cache."""
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    return event_data["attendees"]


def get_waitlist(event_name: str) -> list[WaitlistEntry]:
    """Gets the list of WaitlistEntry objects for a specific event from cache."""
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    return event_data["waitlist"]


def has_complete_attendee_numbers(event_name: str) -> bool:
    """Return True when every current attendee slot has a unique stored number."""
    return _has_complete_attendee_numbers(get_responses(event_name))


def get_max_attendee_number(event_name: str) -> int:
    """Return the highest currently stored attendee number for an event."""
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    return _max_attendee_number(event_data)


def build_attendee_report_rows(event_name: str) -> list[AttendeeReportRow]:
    """Build CSV-ready attendee report rows sorted by stored attendee number."""
    responses = get_responses(event_name)
    rows: list[AttendeeReportRow] = []

    for response in responses:
        if response.attendee_number is not None:
            rows.append(
                AttendeeReportRow(
                    attendee_number=response.attendee_number,
                    name=get_effective_display_name(response),
                    type="primary",
                    registered_by_username=response.username,
                    registered_by_display_name=response.display_name or "",
                    registered_by_user_id=response.user_id,
                    guest_index=None,
                    drink=_drink_for(response, 0),
                )
            )

        for index in range(response.extra_people):
            attendee_number = _number_for_extra(response, index)
            if attendee_number is None:
                continue

            raw_name = response.extras_names[index] if index < len(response.extras_names) else ""
            guest_name = raw_name.strip() if isinstance(raw_name, str) else ""
            rows.append(
                AttendeeReportRow(
                    attendee_number=attendee_number,
                    name=guest_name or f"Guest {index + 1}",
                    type="guest",
                    registered_by_username=response.username,
                    registered_by_display_name=response.display_name or "",
                    registered_by_user_id=response.user_id,
                    guest_index=index + 1,
                    drink=_drink_for(response, index + 1),
                )
            )

    return sorted(rows, key=lambda row: row.attendee_number)


def assign_attendee_numbers(event_name: str) -> int:
    """Durably assign close-time numbers without mutating records before the write."""
    all_data = load_responses()
    current = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    event_data = EventData(
        attendees=[replace(response) for response in current["attendees"]], waitlist=current["waitlist"]
    )

    next_number = 1
    for response in sorted(
        event_data["attendees"],
        key=lambda r: (get_effective_display_name(r).casefold(), r.user_id),
    ):
        next_number = _assign_group_numbers(response, next_number)

    _commit_event_data(event_name, event_data, all_data)
    _log.info("Assigned attendee numbers for event %s.", event_name)
    return next_number - 1


def clear_attendee_numbers(event_name: str) -> None:
    """Clear attendee numbers for an event so the next close can assign them from scratch."""
    all_data = load_responses()
    current = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    event_data = EventData(
        attendees=[replace(response) for response in current["attendees"]], waitlist=current["waitlist"]
    )

    for response in event_data["attendees"]:
        _clear_group_numbers(response)

    _commit_event_data(event_name, event_data, all_data)
    _log.info("Cleared attendee numbers for event %s.", event_name)


def add_response(
    event_name: str,
    response: Response,
    *,
    force_attendee_number: bool = False,
    attendee_number_start: int | None = None,
) -> int | None:
    """Adds a response to the specified event's attendees.

    Raises:
        DuplicateResponseError: If the user has already responded to this event or is on the waitlist.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    # Check for duplicate response in attendees
    if any(r.user_id == response.user_id for r in event_data["attendees"]):
        _log.warning("User %s already responded to event %s. Raising error.", response.user_id, event_name)
        raise DuplicateResponseError(event_name, response.user_id)

    # Check if user is on waitlist
    if any(e.user_id == response.user_id for e in event_data["waitlist"]):
        _log.warning("User %s is on waitlist for event %s. Cannot add to responses.", response.user_id, event_name)
        raise DuplicateResponseError(event_name, response.user_id)

    # If no duplicate, proceed with adding
    assigned_max_number = None
    original_response = response
    if force_attendee_number or _event_has_attendee_numbers(event_data):
        response = replace(response)
        start_number = attendee_number_start if attendee_number_start is not None else _next_attendee_number(event_data)
        assigned_max_number = _assign_group_numbers(response, start_number) - 1

    _commit_event_data(
        event_name, EventData(attendees=[*event_data["attendees"], response], waitlist=event_data["waitlist"]), all_data
    )
    if assigned_max_number is not None:
        original_response.attendee_number = response.attendee_number
        original_response.extras_attendee_numbers = response.extras_attendee_numbers
    _log.info("Added response from user %s to event %s.", response.user_id, event_name)
    return assigned_max_number


def update_attendee_names(
    event_name: str,
    user_id: int,
    expected_timestamp: datetime,
    expected_display_name: str | None,
    expected_extra_people: int,
    expected_extras_names: list[str | None],
    *,
    display_name: str | None = None,
    guest_names: list[tuple[int, str]] | None = None,
) -> AttendeeNameUpdate:
    """Replace only the edited names on the current confirmed attendee record.

    ``expected_*`` is the raw snapshot captured when the editor opened. It must
    still match the live record immediately before the commit, so concurrent
    name or guest-slot changes, a rejoin, or a demotion are rejected instead of
    being overwritten with a stale form. Unedited fields and names are copied
    verbatim; missing legacy guest slots stay missing unless a later slot is
    edited and needs positional materialisation.

    Raises:
        RegistrationChangedError: If the registration identity or snapshot no longer matches.
        ValueError: If the expected snapshot or edited values are malformed.
    """
    if expected_timestamp.tzinfo is None:
        raise ValueError("Registration timestamp requires a timezone")
    if type(expected_extra_people) is not int or not (0 <= expected_extra_people <= MAX_EXTRA_PEOPLE):
        raise ValueError(f"Guest count must be between 0 and {MAX_EXTRA_PEOPLE}")
    if not isinstance(expected_extras_names, list) or len(expected_extras_names) < expected_extra_people:
        raise ValueError("Expected guest names must cover the guest count")
    for index, slot in enumerate(expected_extras_names):
        if index < expected_extra_people:
            if slot is not None and not isinstance(slot, str):
                raise ValueError("Expected guest names must be strings or null")
        elif not isinstance(slot, str):
            raise ValueError("Expected surplus guest names must be strings")
    if display_name is None and not guest_names:
        raise ValueError("No name changes supplied")
    normalized_display_name = (
        _normalized_edited_name(display_name, MAX_DISPLAY_NAME_LENGTH, "Attendee name")
        if display_name is not None
        else None
    )
    normalized_guests: list[tuple[int, str]] = []
    seen_indices: set[int] = set()
    for item in guest_names or []:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("Guest name changes must be index/value pairs")
        index, value = item
        if type(index) is not int or not (0 <= index < expected_extra_people):
            raise ValueError("Guest index is out of range")
        if index in seen_indices:
            raise ValueError("Guest index was edited more than once")
        seen_indices.add(index)
        normalized_guests.append((index, _normalized_edited_name(value, MAX_GUEST_NAME_LENGTH, "Guest name")))

    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    current = next((r for r in event_data["attendees"] if r.user_id == user_id), None)
    if current is None:
        raise RegistrationChangedError()

    current_timestamp = current.timestamp.replace(tzinfo=UTC) if current.timestamp.tzinfo is None else current.timestamp
    expected = expected_timestamp.astimezone(UTC)
    if (
        current_timestamp.astimezone(UTC).replace(microsecond=current_timestamp.microsecond // 1000 * 1000)
        != expected.replace(microsecond=expected.microsecond // 1000 * 1000)
        or current.display_name != expected_display_name
        or current.extra_people != expected_extra_people
    ):
        raise RegistrationChangedError()
    # The snapshot covers every existing slot plus any surplus legacy entries, so
    # a concurrent change to any guest name (not only the rendered slots) conflicts.
    current_extras_snapshot = [
        current.extras_names[index] if index < len(current.extras_names) else None
        for index in range(expected_extra_people)
    ]
    current_extras_snapshot.extend(current.extras_names[expected_extra_people:])
    if current_extras_snapshot != expected_extras_names:
        raise RegistrationChangedError()

    # Capture persisted differences before staging; trim-equivalent edits are no-ops.
    persisted_changes: list[AttendeeNameChange] = []
    if normalized_display_name is not None and normalized_display_name != current.display_name:
        persisted_changes.append(AttendeeNameChange(None, current.display_name, normalized_display_name))
    for index, value in normalized_guests:
        current_slot = current.extras_names[index] if index < len(current.extras_names) else None
        if value != current_slot:
            persisted_changes.append(AttendeeNameChange(index + 1, current_slot, value))

    if not persisted_changes:
        _log.info("No persisted name changes for user %s in event %s.", user_id, event_name)
        return AttendeeNameUpdate(registration=current, changes=())

    # Detach the record and names list from the live cache before writing.
    staged_names = list(current.extras_names)
    for index, value in normalized_guests:
        while len(staged_names) <= index:
            staged_names.append("")
        staged_names[index] = value
    updated = replace(
        current,
        display_name=normalized_display_name if display_name is not None else current.display_name,
        extras_names=staged_names,
    )
    _commit_event_data(
        event_name,
        EventData(
            attendees=[updated if response is current else response for response in event_data["attendees"]],
            waitlist=event_data["waitlist"],
        ),
        all_data,
    )
    _log.info("Updated attendee names for user %s in event %s.", user_id, event_name)
    return AttendeeNameUpdate(registration=updated, changes=tuple(persisted_changes))


def remove_response(event_name: str, user_id: int) -> Response:
    """Removes and returns the response for a given user from the specified event's attendees.

    Raises:
        ResponseNotFoundError: If no response is found for the given user and event.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    removed_response = next((r for r in event_data["attendees"] if r.user_id == user_id), None)

    # Check if any response was actually removed
    if removed_response is None:
        # No response found for the user, raise error
        _log.warning("No response found for user %s in event %s to remove. Raising error.", user_id, event_name)
        raise ResponseNotFoundError(event_name, user_id)
    else:
        # Response found and removed, update the cache and save
        _commit_event_data(
            event_name,
            EventData(
                attendees=[r for r in event_data["attendees"] if r.user_id != user_id], waitlist=event_data["waitlist"]
            ),
            all_data,
        )
        _log.info("Removed response from user %s for event %s.", user_id, event_name)
        return removed_response


def add_to_waitlist(event_name: str, entry: WaitlistEntry) -> None:
    """Adds an entry to the waitlist for the specified event.

    Raises:
        DuplicateResponseError: If the user is already on the waitlist or has already responded to this event.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    # Check for duplicate entry in waitlist
    if any(e.user_id == entry.user_id for e in event_data["waitlist"]):
        _log.warning("User %s already on waitlist for event %s. Raising error.", entry.user_id, event_name)
        raise DuplicateResponseError(event_name, entry.user_id)

    # Check if user has already responded
    if any(r.user_id == entry.user_id for r in event_data["attendees"]):
        _log.warning("User %s already responded to event %s. Cannot add to waitlist.", entry.user_id, event_name)
        raise DuplicateResponseError(event_name, entry.user_id)

    # If no duplicate, proceed with adding
    _commit_event_data(
        event_name, EventData(attendees=event_data["attendees"], waitlist=[*event_data["waitlist"], entry]), all_data
    )
    _log.info("Added user %s to waitlist for event %s.", entry.user_id, event_name)


def remove_from_waitlist(event_name: str, user_id: int) -> None:
    """Removes an entry from the waitlist for the specified event.

    Raises:
        ResponseNotFoundError: If the user is not found on the waitlist.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    initial_count = len(event_data["waitlist"])
    staged_waitlist = [e for e in event_data["waitlist"] if e.user_id != user_id]

    if len(staged_waitlist) == initial_count:
        _log.warning("No waitlist entry found for user %s in event %s. Raising error.", user_id, event_name)
        raise ResponseNotFoundError(event_name, user_id)
    else:
        _commit_event_data(event_name, EventData(attendees=event_data["attendees"], waitlist=staged_waitlist), all_data)
        _log.info("Removed user %s from waitlist for event %s.", user_id, event_name)


def _commit_event_data(event_name: str, staged: EventData, all_data: dict[str, EventData] | None = None) -> None:
    """Publish the cache change only after the single-file replacement succeeds."""
    if all_data is None:
        all_data = load_responses()
    if "payment_expiry_backfill" not in staged:
        pending = all_data.get(event_name, {}).get("payment_expiry_backfill")
        if pending is not None:
            staged = {**staged, "payment_expiry_backfill": pending}
    atomic_write_json(
        get_config()["RESPONSES_FILE"],
        {**all_data, event_name: staged},
        indent=4,
        cls=DataclassJSONEncoder,
        ensure_ascii=False,
    )
    all_data[event_name] = staged


def promote_waitlist_response(
    event: "Event",
    user_id: int | None = None,
    *,
    expected_timestamp: datetime | None = None,
) -> Response | None:
    """Persist a FIFO or selected promotion without an unregistered snapshot.

    Stage the move so failed response writes leave the cache and queue unchanged.
    Save event numbering after the response commit, as for add_response_for_event.
    Deferred signups get a new payment choice; legacy entries retain a valid saved method.
    Validation and commit contain no await, so competing asyncio callers cannot
    promote the same entry twice or reset an already-confirmed user's deadline.
    """
    from offkai_bot.data.event import persist_event_changes

    all_data = load_responses()
    event_data = all_data.get(event.event_name, EventData(attendees=[], waitlist=[]))
    if user_id is None:
        if not event_data["waitlist"]:
            return None
        index = 0
    else:
        index = next((i for i, entry in enumerate(event_data["waitlist"]) if entry.user_id == user_id), None)
        if index is None:
            raise ResponseNotFoundError(event.event_name, user_id)
    entry = event_data["waitlist"][index]
    if expected_timestamp is not None:
        # Match the registration displayed by the frontend at millisecond precision.
        current = entry.timestamp.replace(tzinfo=UTC) if entry.timestamp.tzinfo is None else entry.timestamp
        current = current.astimezone(UTC)
        if current.replace(microsecond=current.microsecond // 1000 * 1000) != expected_timestamp.astimezone(UTC):
            raise RegistrationChangedError()
    if any(response.user_id == entry.user_id for response in event_data["attendees"]):
        raise DuplicateResponseError(event.event_name, entry.user_id)
    response = Response(**asdict(entry))
    methods = (event.signup_form or {}).get("payment_methods", {})
    legacy_selected_method = not entry.payment_selection_demotes and entry.payment_method in methods
    if methods and not legacy_selected_method:
        # Only newly recorded deferred-payment signups can lose a place on expiry.
        # Legacy waitlist entries may already have paid through the old proof flow.
        response.payment_selection_demotes = entry.payment_selection_demotes and entry.payment_method is None
        response.payment_method = None
        response.payment_selection_expires_at = datetime.now(UTC) + timedelta(hours=72)
    assigned_max_number = None
    if not event.open or _event_has_attendee_numbers(event_data):
        start_number = _next_attendee_number(event_data)
        if not event.open:
            start_number = (
                max(
                    event.max_attendee_number or 0,
                    event.closed_attendance_count or 0,
                    _max_attendee_number(event_data),
                )
                + 1
            )
        assigned_max_number = _assign_group_numbers(response, start_number) - 1
    staged = EventData(
        attendees=[*event_data["attendees"], response],
        waitlist=[*event_data["waitlist"][:index], *event_data["waitlist"][index + 1 :]],
    )
    _commit_event_data(event.event_name, staged)
    if assigned_max_number is not None:
        persist_event_changes(event, max_attendee_number=assigned_max_number)
    return response


def expire_payment_selections(event: "Event", now: datetime) -> list[Response]:
    """Atomically move expired, unchosen groups to the queue tail; never count this as withdrawal."""
    if (
        event.archived
        or event.interest_check
        or event.event_datetime <= now
        or not (event.signup_form or {}).get("payment_methods")
    ):
        return []
    data = load_responses().get(event.event_name, EventData(attendees=[], waitlist=[]))
    expired = [
        entry
        for entry in data["attendees"]
        if entry.payment_selection_demotes
        and entry.payment_method is None
        and entry.payment_selection_expires_at is not None
        and entry.payment_selection_expires_at <= now
    ]
    if not expired:
        return []
    expired_ids = {entry.user_id for entry in expired}
    fields = WaitlistEntry.__dataclass_fields__
    queued = [
        WaitlistEntry(**{key: value for key, value in asdict(entry).items() if key in fields}) for entry in expired
    ]
    previous = data.get("payment_expiry_backfill")
    excluded = expired_ids | (set(previous["excluded_user_ids"]) if previous else set())
    eligible = set(previous["eligible_registrations"]) if previous else set()
    eligible.update(registration_key(entry) for entry in data["waitlist"] if entry.user_id not in excluded)
    pending = PaymentExpiryBackfill(
        # Absolute target prevents retries from counting already-filled places twice, including unlimited events.
        target_count=max(
            sum(1 + entry.extra_people for entry in data["attendees"]),
            previous["target_count"] if previous else 0,
        ),
        eligible_registrations=sorted(eligible),
        excluded_user_ids=sorted(excluded),
    )
    _commit_event_data(
        event.event_name,
        EventData(
            attendees=[entry for entry in data["attendees"] if entry.user_id not in expired_ids],
            waitlist=[*data["waitlist"], *queued],
            payment_expiry_backfill=pending,
        ),
    )
    return expired


def get_payment_expiry_backfill(event_name: str) -> PaymentExpiryBackfill | None:
    return load_responses().get(event_name, {}).get("payment_expiry_backfill")


def clear_payment_expiry_backfill(event_name: str, expected: PaymentExpiryBackfill) -> None:
    data = load_responses().get(event_name)
    if data is not None and data.get("payment_expiry_backfill") is expected:
        # A newer expiry may replace this request while its promotion DMs are pending.
        _commit_event_data(event_name, {**data, "payment_expiry_backfill": None})


def complete_payment_selection(
    event_name: str, user_id: int, timestamp: datetime, expires_at: datetime, method: str
) -> Response:
    """Save a promoted attendee's choice atomically; reject expired or replaced registrations."""
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))
    response = next((r for r in event_data["attendees"] if r.user_id == user_id), None)
    if (
        response is None
        or response.timestamp != timestamp
        or response.payment_selection_expires_at != expires_at
        or datetime.now(UTC) >= expires_at
    ):
        raise ValueError("This payment choice has expired, been completed, or belongs to an old registration.")
    updated = replace(
        response, payment_method=method, payment_selection_expires_at=None, payment_selection_demotes=False
    )
    staged = EventData(
        attendees=[updated if r is response else r for r in event_data["attendees"]],
        waitlist=event_data["waitlist"],
    )
    _commit_event_data(event_name, staged)
    return updated


def prepare_payment_selection(event: "Event", entry: Response) -> Response:
    """Repair legacy methods; preserve live/demoting deadlines, reopen expired exempt choices on resend."""
    methods = (event.signup_form or {}).get("payment_methods", {})
    event_data = load_responses().get(event.event_name, EventData(attendees=[], waitlist=[]))
    current = next(
        (r for r in event_data["attendees"] if r.user_id == entry.user_id and r.timestamp == entry.timestamp), None
    )
    if current is None or not methods or current.payment_method in methods:
        return current or entry
    now = datetime.now(UTC)
    expiry = current.payment_selection_expires_at
    if current.payment_method is None and expiry is not None and (current.payment_selection_demotes or expiry > now):
        return current
    if expiry is None or (not current.payment_selection_demotes and expiry <= now):
        expiry = now + timedelta(hours=72)
    updated = replace(
        current,
        payment_method=None,
        payment_selection_demotes=False,
        payment_selection_expires_at=expiry,
    )
    _commit_event_data(
        event.event_name,
        EventData(
            attendees=[updated if r is current else r for r in event_data["attendees"]],
            waitlist=event_data["waitlist"],
        ),
    )
    return updated


def promote_from_waitlist(event_name: str) -> WaitlistEntry | None:
    """
    Removes and returns the first entry from the waitlist (FIFO).
    Returns None if the waitlist is empty.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    if not event_data["waitlist"]:
        return None

    first_entry = event_data["waitlist"][0]
    _commit_event_data(
        event_name, EventData(attendees=event_data["attendees"], waitlist=event_data["waitlist"][1:]), all_data
    )
    _log.info("Promoted user %s from waitlist for event %s.", first_entry.user_id, event_name)

    return first_entry


def promote_specific_from_waitlist(event_name: str, user_id: int) -> tuple[WaitlistEntry, int]:
    """
    Removes a specific user from the waitlist by user_id.
    Returns the entry and its original waitlist index, so a rollback can
    restore it at the same position instead of jumping the queue.

    Raises:
        ResponseNotFoundError: If the user is not found on the waitlist.
        DuplicateResponseError: If the user already has an attendee record, so a
            subsequent add_response would fail after the waitlist entry was removed.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    if any(r.user_id == user_id for r in event_data["attendees"]):
        _log.warning(
            "User %s is already an attendee of event %s. Refusing to remove waitlist entry.", user_id, event_name
        )
        raise DuplicateResponseError(event_name, user_id)

    for i, entry in enumerate(event_data["waitlist"]):
        if entry.user_id == user_id:
            promoted_entry = entry
            _commit_event_data(
                event_name,
                EventData(
                    attendees=event_data["attendees"],
                    waitlist=[*event_data["waitlist"][:i], *event_data["waitlist"][i + 1 :]],
                ),
                all_data,
            )
            _log.info("Promoted specific user %s from waitlist for event %s.", user_id, event_name)
            return promoted_entry, i

    raise ResponseNotFoundError(event_name, user_id)


def restore_waitlist_entry(event_name: str, entry: WaitlistEntry, position: int = 0) -> None:
    """
    Re-inserts a previously popped waitlist entry, used to roll back a failed promotion.
    Does nothing if the user is already back on the waitlist.
    """
    all_data = load_responses()
    event_data = all_data.get(event_name, EventData(attendees=[], waitlist=[]))

    if any(e.user_id == entry.user_id for e in event_data["waitlist"]):
        return

    staged_waitlist = list(event_data["waitlist"])
    staged_waitlist.insert(position, entry)
    _commit_event_data(event_name, EventData(attendees=event_data["attendees"], waitlist=staged_waitlist), all_data)
    _log.info("Restored user %s to waitlist for event %s after failed promotion.", entry.user_id, event_name)


def calculate_attendance(
    event_name: str, *, nicknames: bool = False, drinks: bool = False, sort: bool = False
) -> tuple[int, list[str]]:
    """
    Calculates the total attendance count and generates a list of attendee names
    (including extras) for a given event.

    Args:
        event_name: The name of the event.
        nicknames: If True, include the Discord username for disambiguation.
        drinks: If True, append each attendee's drink choice.
        sort: If True, sort based on the main user and bundle extras after them.
            Ignored for completely numbered events so stored numbers display in sequence.

    Returns:
        A tuple containing:
            - total_count (int): The total number of attendees including extras.
            - attendee_names (list[str]): A list of formatted attendee names.

    Raises:
        NoResponsesFoundError: If no responses are found for the event.
    """
    responses = get_responses(event_name)
    if not responses:
        raise NoResponsesFoundError(event_name)

    has_complete_attendee_numbers = _has_complete_attendee_numbers(responses)
    if sort and not has_complete_attendee_numbers:
        responses = sorted(
            responses,
            key=lambda r: (get_effective_display_name(r).casefold(), r.user_id),
        )

    attendee_names: list[str] = []
    total_count = 0
    for response in responses:
        # Add the main person
        name = format_organizer_name(response, nicknames=nicknames)
        if drinks:
            drink = response.drinks[0] if response.drinks else "N/A"
            name = f"{name} - {drink}"
        attendee_names.append(NumberedAttendeeName(name, response.attendee_number))
        total_count += 1

        # Add extra people
        for i in range(response.extra_people):
            raw_name = response.extras_names[i] if i < len(response.extras_names) else ""
            stripped_name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = stripped_name or " "
            name += f" ({get_effective_display_name(response)} +{i + 1})"
            if drinks:
                drink_index = i + 1
                drink = response.drinks[drink_index] if drink_index < len(response.drinks) else "N/A"
                name = f"{name} - {drink}"
            attendee_names.append(NumberedAttendeeName(name, _number_for_extra(response, i)))
            total_count += 1

    if has_complete_attendee_numbers:
        attendee_names = sorted(attendee_names, key=lambda name: getattr(name, "attendee_number"))

    _log.info("Calculated attendance for '%s': %s attendees.", event_name, total_count)
    return total_count, attendee_names


def calculate_waitlist(event_name: str, *, nicknames: bool = False, sort: bool = False) -> tuple[int, list[str]]:
    """
    Calculates the total waitlist count and generates a list of waitlisted names
    (including extras) for a given event.

    Args:
        event_name: The name of the event.
        nicknames: If True, include the Discord username for disambiguation.
        sort: If True, sort based on the main user and bundle extras after them.

    Returns:
        A tuple containing:
            - total_count (int): The total number of waitlisted people including extras.
            - waitlisted_names (list[str]): A list of formatted waitlisted names.

    Raises:
        NoWaitlistEntriesFoundError: If no waitlist entries are found for the event.
    """
    entries = get_waitlist(event_name)
    if not entries:
        raise NoWaitlistEntriesFoundError(event_name)

    if sort:
        entries = sorted(
            entries,
            key=lambda e: (get_effective_display_name(e).casefold(), e.user_id),
        )

    waitlisted_names = []
    total_count = 0
    for entry in entries:
        name = format_organizer_name(entry, nicknames=nicknames)
        waitlisted_names.append(name)
        total_count += 1

        for i in range(entry.extra_people):
            raw_name = entry.extras_names[i] if i < len(entry.extras_names) else ""
            stripped_name = raw_name.strip() if isinstance(raw_name, str) else ""
            name = stripped_name or " "
            name += f" ({get_effective_display_name(entry)} +{i + 1})"
            waitlisted_names.append(name)
            total_count += 1

    _log.info("Calculated waitlist for '%s': %s waitlisted.", event_name, total_count)
    return total_count, waitlisted_names


def calculate_drinks(event_name: str) -> tuple[int, dict[str, int]]:
    responses = get_responses(event_name)
    if not responses:
        raise NoResponsesFoundError(event_name)

    drinks = []

    for response in responses:
        # Add drinks (if required)
        if len(response.drinks) > 0:
            drinks.extend(response.drinks)

    if len(drinks) > 0:
        drinks_count = Counter(drinks)

        _log.info("Calculated %s drink(s) for '%s'.", len(drinks), event_name)

        return len(drinks), drinks_count
    else:
        _log.info("No drinks found for '%s'.", event_name)
        return 0, {}
