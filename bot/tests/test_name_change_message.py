"""Pure bilingual formatting tests for the post-save name-change DM."""

from datetime import UTC, datetime

from offkai_bot.data.event import Event
from offkai_bot.data.response import AttendeeNameChange
from offkai_bot.interactions import build_attendee_names_changed_message

EVENT = Event("Offkai Night", "Venue", "Address", "", datetime(3026, 1, 1, tzinfo=UTC))


def test_bilingual_message_lists_only_primary_and_guest_changes_in_order():
    message = build_attendee_names_changed_message(
        EVENT,
        (
            AttendeeNameChange(None, "Old Name", "New Name"),
            AttendeeNameChange(2, None, "New Guest"),
        ),
    )
    assert message == (
        "✏️ The organizer changed your registration for **Offkai Night**:\n"
        "👤 Name: ~~Old Name~~ → **New Name**\n"
        "👥 Guest 2: ~~(no name)~~ → **New Guest**\n"
        "\n"
        "✏️ 主催者が**Offkai Night**の登録内容を変更しました：\n"
        "👤 名前：~~Old Name~~ → **New Name**\n"
        "👥 同伴者2：~~（名前なし）~~ → **New Guest**"
    )


def test_blank_and_missing_old_names_use_the_localized_no_name_marker():
    message = build_attendee_names_changed_message(
        EVENT,
        (
            AttendeeNameChange(None, "", "Primary New"),
            AttendeeNameChange(1, "   ", "Guest New"),
        ),
    )
    assert "👤 Name: ~~(no name)~~ → **Primary New**" in message
    assert "👥 Guest 1: ~~(no name)~~ → **Guest New**" in message
    assert "👤 名前：~~（名前なし）~~ → **Primary New**" in message
    assert "👥 同伴者1：~~（名前なし）~~ → **Guest New**" in message


def test_no_changes_produces_no_message_and_markdown_or_newlines_are_safe():
    assert build_attendee_names_changed_message(EVENT, ()) == ""
    escaped = build_attendee_names_changed_message(
        Event("Offkai *Night*", "Venue", "Address", "", EVENT.event_datetime),
        (
            AttendeeNameChange(None, "Old_Name", "New@everyone\nLine"),
            AttendeeNameChange(1, "~Guest~", "`Guest`"),
        ),
    )
    assert "**Offkai \\*Night\\***" in escaped
    assert "Old\\_Name" in escaped
    assert "New@everyone Line" in escaped
    assert "\\~Guest\\~" in escaped
    assert "\\`Guest\\`" in escaped


def test_message_is_bounded_for_long_legacy_names_and_multibyte_text():
    changes = tuple(AttendeeNameChange(index if index else None, "旧" * 5_000, "新" * 5_000) for index in range(6))
    message = build_attendee_names_changed_message(
        Event("E" * 1_000, "Venue", "Address", "", EVENT.event_datetime),
        changes,
    )
    assert len(message.encode("utf-16-le")) // 2 <= 2_000
    assert "✏️ The organizer changed your registration" in message
    assert "✏️ 主催者が" in message
    assert "…" in message
