import asyncio
import hashlib
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from weakref import WeakValueDictionary

import discord
from aiohttp import ClientError
from discord import ui

from offkai_bot.data.event import Event
from offkai_bot.data.ranking import can_rank_message_sent, decrease_rank, get_rank, mark_achieved_rank, update_rank
from offkai_bot.data.response import (
    AttendeeNameChange,
    Response,
    WaitlistEntry,
    add_response,
    add_to_waitlist,
    complete_payment_selection,
    get_effective_display_name,
    get_responses,
    get_waitlist,
    prepare_payment_selection,
    promote_waitlist_response,
    registration_key,
    remove_from_waitlist,
    remove_response,
)
from offkai_bot.errors import (
    DuplicateResponseError,
    ResponseNotFoundError,
)
from offkai_bot.messages import MILESTONE_MESSAGES
from offkai_bot.role_management import assign_event_role, remove_event_role
from offkai_bot.util import build_checkin_url, send_interaction_error

_log = logging.getLogger(__name__)
_active_signups: dict[tuple[str, int], "SignupContinuation"] = {}


# --- Custom Exception for Validation ---
class ValidationError(Exception):
    """Custom exception for modal validation errors."""

    pass


# --- Helper ---
async def error_message(interaction: discord.Interaction, message: str):
    await send_interaction_error(interaction, f"❌ {message}", logger=_log)


def _payment_methods(event: Event) -> dict[str, str]:
    return (event.signup_form or {}).get("payment_methods", {})


@dataclass(frozen=True)
class SignupContext:
    channel: (
        discord.abc.GuildChannel
        | discord.Thread
        | discord.DMChannel
        | discord.GroupChannel
        | discord.PartialMessageable
        | None
    )
    guild: discord.Guild | None
    user: discord.User | discord.Member

    @classmethod
    def from_interaction(cls, interaction: discord.Interaction) -> "SignupContext":
        return cls(interaction.channel, interaction.guild, interaction.user)

    @property
    def channel_id(self) -> int | None:
        return self.channel.id if self.channel else None


_membership_locks: WeakValueDictionary[tuple[str, int], asyncio.Lock] = WeakValueDictionary()


def _membership_lock(event: Event, user_id: int) -> asyncio.Lock:
    key = event.event_name, user_id
    lock = _membership_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _membership_locks[key] = lock
    return lock


def _registration_timestamp(event: Event, user_id: int) -> datetime | None:
    return next(
        (
            entry.timestamp
            for entry in [*get_responses(event.event_name), *get_waitlist(event.event_name)]
            if entry.user_id == user_id
        ),
        None,
    )


async def remove_event_thread_member(event: Event, thread: discord.Thread, user: Any) -> None:
    async with _membership_lock(event, user.id):
        # A withdrawal's Discord work may resume after the user has rejoined.
        if _registration_timestamp(event, user.id) is None:
            await thread.remove_user(user)


async def _add_user_to_event_thread(event: Event, context: SignupContext, client: discord.Client) -> bool:
    timestamp = _registration_timestamp(event, context.user.id)
    if timestamp is None:
        return False
    async with _membership_lock(event, context.user.id):
        return await _join_registered_thread(event, context, client, timestamp)


async def _join_registered_thread(
    event: Event, context: SignupContext, client: discord.Client, timestamp: datetime
) -> bool:
    """Join the saved signup's thread, including threads auto-archived during a DM step."""
    thread = context.channel
    try:
        if not isinstance(thread, discord.Thread) or (event.thread_id and thread.id != event.thread_id):
            # Local import avoids the event_actions -> interactions import cycle.
            from offkai_bot.event_actions import fetch_thread_for_event

            thread = await fetch_thread_for_event(client, event)

        for attempt in range(2):
            if _registration_timestamp(event, context.user.id) != timestamp:
                return False
            if event.archived or (thread.archived and thread.locked):
                _log.warning(
                    "Not adding user %s to archived event or locked archived thread %s for '%s'.",
                    context.user.id,
                    thread.id,
                    event.event_name,
                )
                return False
            if thread.archived:
                thread = await thread.edit(archived=False)
            try:
                if _registration_timestamp(event, context.user.id) != timestamp:
                    return False
                await thread.add_user(context.user)
                if _registration_timestamp(event, context.user.id) is None:
                    await thread.remove_user(context.user)
                    return False
                return True
            except discord.HTTPException as error:
                if error.code != 50083 or attempt:
                    raise
                # Refresh once when the gateway lags behind Discord's archive state.
                # The member PUT is idempotent; never unlock a moderator-locked thread.
                refreshed = await client.fetch_channel(thread.id)
                if not isinstance(refreshed, discord.Thread):
                    raise
                thread = refreshed
    except Exception as error:
        _log.error(
            "Failed to add user %s to event thread %s for '%s': %s",
            context.user.id,
            event.thread_id or context.channel_id,
            event.event_name,
            error,
        )

    return False


def validate_extra_people_input(extra_people_str: str) -> int:
    """Validates an extra-people modal input.

    Returns:
        int specifying the number of extra people.

    Raises:
        ValidationError: If input is invalid.
    """
    if not extra_people_str.isdigit() or not (0 <= int(extra_people_str) <= 5):
        raise ValidationError("Extra people must be a number between 0 and 5.")
    return int(extra_people_str)


def resolve_submitted_display_name(
    submitted_name: str,
    discord_display_name: str,
    username: str,
) -> str:
    """Resolve a submitted preferred name to the event-specific snapshot value."""

    submitted = submitted_name.strip() if isinstance(submitted_name, str) else ""
    discord_name = discord_display_name.strip() if isinstance(discord_display_name, str) else ""
    return submitted or discord_name or username


async def modal_error_message(interaction: discord.Interaction, event_name: str, message: str, *, in_dm: bool = False):
    dm_message = f"❌ I couldn't process your response for **{event_name}**.\n\n{message}"
    if in_dm:
        await send_interaction_error(interaction, dm_message, dm_fallback=True, logger=_log)
        return
    try:
        await interaction.user.send(dm_message)
    except (discord.Forbidden, discord.HTTPException) as e:
        _log.warning(
            "Could not DM modal submission error to user %s for event '%s': %s",
            interaction.user.id,
            event_name,
            e,
        )
        await error_message(interaction, message)
        return

    await interaction.response.send_message(
        "❌ I couldn't process your response. I've sent you a DM with the details.",
        ephemeral=True,
    )


def get_current_attendance_count(event_name: str) -> int:
    """Calculate total current attendance including extra people."""
    responses = get_responses(event_name)
    return sum(1 + response.extra_people for response in responses)


def is_event_at_capacity(event: Event) -> bool:
    """Check if the event has reached its maximum capacity."""
    if event.max_capacity is None:
        return False  # Unlimited capacity

    current_count = get_current_attendance_count(event.event_name)
    return current_count >= event.max_capacity


def would_exceed_capacity(event: Event, num_people: int) -> bool:
    """Check if adding num_people would exceed the event's capacity."""
    if event.max_capacity is None:
        return False  # Unlimited capacity

    current_count = get_current_attendance_count(event.event_name)
    return (current_count + num_people) > event.max_capacity


def get_remaining_capacity(event: Event) -> int | None:
    """Get the number of remaining spots. Returns None if unlimited capacity."""
    if event.max_capacity is None:
        return None

    current_count = get_current_attendance_count(event.event_name)
    return max(0, event.max_capacity - current_count)


# Name-change DMs are bounded so long legacy names or multibyte text can never
# exceed Discord's 2000-character message limit. Try to show full values first,
# then lower the per-value bound until the bilingual message fits comfortably.
_NAME_CHANGE_LIMITS = (160, 120, 96, 80, 64, 48, 32, 24, 16, 8)
_NAME_CHANGE_EVENT_LIMIT = 100
_NAME_CHANGE_MESSAGE_LIMIT = 1900
_MISSING_NAME_EN = "(no name)"
_MISSING_NAME_JA = "（名前なし）"


def _bounded_notification_value(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _escaped_notification_name(value: str | None, limit: int, *, japanese: bool) -> str:
    text = _bounded_notification_value(value, limit)
    if text is None:
        text = _MISSING_NAME_JA if japanese else _MISSING_NAME_EN
    return discord.utils.escape_markdown(text)


def _discord_message_length(message: str) -> int:
    # Discord bounds message content in UTF-16 code units, not code points.
    return len(message.encode("utf-16-le")) // 2


def _render_attendee_names_changed_message(
    event_name: str,
    changes: Sequence[AttendeeNameChange],
    name_limit: int,
) -> str:
    bounded_event_name = _bounded_notification_value(event_name, _NAME_CHANGE_EVENT_LIMIT) or ""
    escaped_event = discord.utils.escape_markdown(bounded_event_name)
    english = [f"✏️ The organizer changed your registration for **{escaped_event}**:"]
    japanese = [f"✏️ 主催者が**{escaped_event}**の登録内容を変更しました："]
    for change in changes:
        old_en = _escaped_notification_name(change.old_name, name_limit, japanese=False)
        new_en = _escaped_notification_name(change.new_name, name_limit, japanese=False)
        old_ja = _escaped_notification_name(change.old_name, name_limit, japanese=True)
        new_ja = _escaped_notification_name(change.new_name, name_limit, japanese=True)
        if change.guest_number is None:
            english.append(f"👤 Name: ~~{old_en}~~ → **{new_en}**")
            japanese.append(f"👤 名前：~~{old_ja}~~ → **{new_ja}**")
        else:
            english.append(f"👥 Guest {change.guest_number}: ~~{old_en}~~ → **{new_en}**")
            japanese.append(f"👥 同伴者{change.guest_number}：~~{old_ja}~~ → **{new_ja}**")
    return "\n".join(english) + "\n\n" + "\n".join(japanese)


def build_attendee_names_changed_message(
    event: Event,
    changes: Sequence[AttendeeNameChange],
    *,
    event_name: str | None = None,
) -> str:
    """Bilingual DM body listing only the persisted name fields that changed.

    ``event_name`` lets the caller use the name captured at commit time so a
    later event mutation cannot change the wording of this committed operation.
    """
    changes = tuple(changes)
    if not changes:
        return ""
    committed_event_name = event.event_name if event_name is None else event_name
    for name_limit in _NAME_CHANGE_LIMITS:
        message = _render_attendee_names_changed_message(committed_event_name, changes, name_limit)
        if _discord_message_length(message) <= _NAME_CHANGE_MESSAGE_LIMIT:
            return message
    return _render_attendee_names_changed_message(committed_event_name, changes, _NAME_CHANGE_LIMITS[-1])


def build_confirmation_message(event: Event, response: Response) -> str:
    """Default (non-custom-form) DM body for a confirmed attendee."""
    recorded_name = get_effective_display_name(response)
    rsvp_url = build_checkin_url(response.user_id, response.event_name)
    rsvp_link_msg = f"\n🔗 **RSVP Page / QR Code:** {rsvp_url}" if rsvp_url else ""
    rsvp_link_msg_jp = f"\n🔗 **RSVPページ / QRコード:** {rsvp_url}" if rsvp_url else ""
    drinks_msg = f"\n🍺 Drinks: {', '.join(response.drinks)}" if response.drinks else ""
    drinks_msg_jp = f"\n🍺 飲み物: {', '.join(response.drinks)}" if response.drinks else ""
    return (
        f"✅ Attendance confirmed for **{event.event_name}**!\n"
        f"👤 Name recorded: {recorded_name}\n"
        f"👥 Bringing: {response.extra_people} extra guest(s)\n"
        f"✔ Behavior Confirmed\n"
        f"✔ Arrival Confirmed"
        f"{drinks_msg}"
        f"{rsvp_link_msg}\n\n"
        f"✅ 参加確定: **{event.event_name}**\n"
        f"👤 登録名: {recorded_name}\n"
        f"👥 同伴者: {response.extra_people}名\n"
        f"✔ 行動確認済み\n"
        f"✔ 到着確認済み"
        f"{drinks_msg_jp}"
        f"{rsvp_link_msg_jp}\n\n"
        f"⚠️ **Important:** Withdrawing after the deadline is strongly discouraged. "
        f"If you withdraw late, you are fully responsible for any consequences, including "
        f"payment requests from the event organizer and potential server moderation action.\n\n"
        f"⚠️ **重要:** 締め切り後の辞退は強くお勧めしません。"
        f"遅れて辞退した場合、主催者からの支払い請求やサーバーのモデレーション措置を含む"
        f"すべての結果に対して、全責任を負います。"
    )


def build_waitlist_message(event: Event, entry: WaitlistEntry) -> str:
    """Default (non-custom-form) DM body for a waitlisted registrant."""
    recorded_name = get_effective_display_name(entry)
    drinks_msg = f"\n🍺 Drinks: {', '.join(entry.drinks)}" if entry.drinks else ""
    drinks_msg_jp = f"\n🍺 飲み物: {', '.join(entry.drinks)}" if entry.drinks else ""
    return (
        f"📋 You've been added to the waitlist for **{event.event_name}**!\n"
        f"👤 Name recorded: {recorded_name}\n"
        f"👥 Bringing: {entry.extra_people} extra guest(s)\n"
        f"✔ Behavior Confirmed\n"
        f"✔ Arrival Confirmed"
        f"{drinks_msg}\n\n"
        f"📋 **{event.event_name}**のウェイトリストに追加されました！\n"
        f"👤 登録名: {recorded_name}\n"
        f"👥 同伴者: {entry.extra_people}名\n"
        f"✔ 行動確認済み\n"
        f"✔ 到着確認済み"
        f"{drinks_msg_jp}\n\n"
        f"You will be automatically added to the event if a spot opens up.\n"
        f"空きが出た場合、自動的にイベントに追加されます。\n\n"
        f"⚠️ **Important:** Withdrawing after the deadline is strongly discouraged. "
        f"If you withdraw late, you are fully responsible for any consequences, including "
        f"payment requests from the event organizer and potential server moderation action.\n\n"
        f"⚠️ **重要:** 締め切り後の辞退は強くお勧めしません。"
        f"遅れて辞退した場合、主催者からの支払い請求やサーバーのモデレーション措置を含む"
        f"すべての結果に対して、全責任を負います。\n\n"
        f"💰 **Note:** If no one drops out and you are still allowed to join the offkai, "
        f"you may be charged extra by the organizers.\n"
        f"💰 **注意:** 誰もキャンセルせず、それでもオフ会への参加が認められた場合、"
        f"主催者から追加料金が請求される場合があります。"
    )


# Prepended to a resent confirmation so the registrant knows why they got it again.
REGISTRATION_UPDATED_NOTICE = (
    "🔄 The organizers updated your registration for **{event_name}**. Your current details are below.",
    "🔄 主催者が**{event_name}**の登録内容を更新しました。現在の登録内容は以下の通りです。",
)


DM_FAILURE_NOTICE = "I couldn't send you a DM. Please enable DMs from this server, then "


async def notify_dm_failure(client: discord.Client, event: Event, user_id: int) -> None:
    """Offer recovery in the event thread without publishing private registration details."""
    from offkai_bot.event_actions import fetch_thread_for_event

    try:
        thread = await fetch_thread_for_event(client, event)
        if event.archived or thread.archived or thread.locked:
            return
        await thread.send(
            f"<@{user_id}> {DM_FAILURE_NOTICE}ask the organizer to resend your confirmation. "
            "Your registration is saved; you do not need to register again.",
            allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=user_id)], roles=False, everyone=False),
        )
    except Exception:
        _log.warning("Could not send DM recovery notice for user %s in event %r", user_id, event.event_name)


async def resend_registration_details(
    event: Event, client: discord.Client, user_ids: set[int] | None = None
) -> tuple[list[str], list[str]]:
    """DM registrants their current details, as recorded now.

    Covers attendees and the waitlist, or only `user_ids` when given.
    Returns (sent, failed) as lists of usernames.
    """
    targets = [*get_responses(event.event_name), *get_waitlist(event.event_name)]
    notice = tuple(line.format(event_name=event.event_name) for line in REGISTRATION_UPDATED_NOTICE)
    sent: list[str] = []
    failed: list[str] = []
    for target in targets:
        entry = target
        if user_ids is not None and entry.user_id not in user_ids:
            continue
        try:
            user = await client.fetch_user(entry.user_id)
            # Expiry or withdrawal can change the roster while Discord resolves the user.
            current = next(
                (
                    r
                    for r in [*get_responses(event.event_name), *get_waitlist(event.event_name)]
                    if r.user_id == entry.user_id
                ),
                None,
            )
            if current is None:
                continue
            entry = current
            if isinstance(entry, Response):
                if entry.payment_method not in _payment_methods(event):
                    entry = prepare_payment_selection(event, entry)
                status = "Attendance confirmed"
                message = build_confirmation_message(event, entry)
            else:
                status = "Waitlisted — attendance is not confirmed"
                message = build_waitlist_message(event, entry)
            await send_signup_reply(user.send, event, entry, status, message, notice=(notice[0], notice[1]))
            sent.append(entry.username)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound, OSError) as e:
            _log.warning("Could not resend details to user %s for '%s': %s", entry.user_id, event.event_name, e)
            failed.append(entry.username)
    return sent, failed


LATE_WITHDRAWAL_WARNING = (
    "⚠️ **Important:** Withdrawing after the deadline is strongly discouraged. "
    "If you withdraw late, you are fully responsible for any consequences, including "
    "payment requests from the event organizer and potential server moderation action.\n\n"
    "⚠️ **重要:** 締め切り後の辞退は強くお勧めしません。"
    "遅れて辞退した場合、主催者からの支払い請求やサーバーのモデレーション措置を含む"
    "すべての結果に対して、全責任を負います。"
)


def build_promotion_message(event: Event, response: Response, *, manual: bool = False) -> str:
    """Default (non-custom-form) DM body for an attendee promoted from the waitlist."""
    rsvp_url = build_checkin_url(response.user_id, event.event_name)
    rsvp_link_msg = f"\n🔗 **RSVP Page / QR Code:** {rsvp_url}" if rsvp_url else ""
    rsvp_link_msg_jp = f"\n🔗 **RSVPページ / QRコード:** {rsvp_url}" if rsvp_url else ""
    if manual:
        intro = (
            f"🎉 Great news! You've been manually promoted from the waitlist for **{event.event_name}**!\n"
            "You are now a confirmed attendee.\n"
        )
        intro_jp = (
            f"🎉 朗報です！**{event.event_name}**のウェイトリストから手動で昇格されました！\n参加が確定しました。\n"
        )
    else:
        intro = (
            f"🎉 Great news! A spot has opened up for **{event.event_name}**!\n"
            "You've been automatically moved from the waitlist to confirmed attendees.\n"
        )
        intro_jp = (
            f"🎉 朗報です！**{event.event_name}**に空きが出ました！\n"
            "ウェイトリストから自動的に参加確定に移動されました。\n"
        )
    return f"{intro}{rsvp_link_msg}\n\n{intro_jp}{rsvp_link_msg_jp}\n\n{LATE_WITHDRAWAL_WARNING}"


async def announce_rank_milestone(channel: object, user_id: int, username: str) -> None:
    """Post the 1st/5th/10th-offkai message once per milestone, if the rank just reached one."""
    rank = get_rank(user_id, username)
    if rank not in MILESTONE_MESSAGES or not can_rank_message_sent(user_id):
        return
    if not isinstance(channel, discord.abc.Messageable):
        return
    try:
        # Milestone messages congratulate the user by mention; opt in to
        # user pings past the client-wide AllowedMentions.none() default.
        await channel.send(
            random.choice(MILESTONE_MESSAGES[rank]).format(user_id=user_id),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
        mark_achieved_rank(user_id)
    except discord.HTTPException as e:
        _log.error("Failed to send milestone message for user %s: %s", user_id, e)


async def credit_promoted_attendance(client: discord.Client, event: Event, response: Response) -> None:
    """Give a promotion the same rank credit as a direct confirmed signup.

    Every way of leaving the attendee list decrements rank, so every way of joining it
    must increment it. Failures here never undo the saved promotion.
    """
    try:
        update_rank(response.user_id, response.username)
        channel = client.get_channel(event.thread_id) if event.thread_id else None
        await announce_rank_milestone(channel, response.user_id, response.username)
    except Exception:
        _log.exception("Rank update failed after promoting user %s in %r", response.user_id, event.event_name)


async def promote_waitlist_batch(
    event: Event,
    client: discord.Client,
    freed_spots: int | None = None,
    *,
    excluded_user_ids: set[int] | None = None,
    eligible_registrations: set[str] | None = None,
    target_attendance: int | None = None,
) -> list[int]:
    """
    Promote users from waitlist to fill available capacity.

    For events without a target capacity (unlimited capacity and never closed),
    freed_spots bounds the promotion to the headcount freed by a withdrawal so
    the confirmed total stays stable. If freed_spots is also None, there is no
    constraint to protect and the entire waitlist is promoted.

    Returns list of promoted user IDs.
    """
    promoted_user_ids: list[int] = []
    # Freeze queue entry objects before any await. Expiry creates new entries, even if timestamps are retained.
    eligible_entries = tuple(get_waitlist(event.event_name))

    # Resolve guild for role assignment
    guild: discord.Guild | None = None
    if event.role_id and event.thread_id:
        channel = client.get_channel(event.thread_id)
        if isinstance(channel, discord.Thread):
            guild = channel.guild

    # Determine the target capacity for promotion
    # If event was closed with a specific count, don't exceed that count
    # Otherwise, use the max_capacity
    target_capacity: int | None = None
    if event.closed_attendance_count is not None:
        # Event was closed with X people, don't exceed min(closed_count, max_capacity)
        if event.max_capacity is not None:
            target_capacity = min(event.closed_attendance_count, event.max_capacity)
        else:
            target_capacity = event.closed_attendance_count
    else:
        # Event is still open or was never closed, use max_capacity
        target_capacity = event.max_capacity

    if target_attendance is not None:
        target_capacity = target_attendance if target_capacity is None else min(target_capacity, target_attendance)
    if target_capacity is None and freed_spots is not None:
        # Unlimited capacity, but a withdrawal freed a specific headcount:
        # backfill exactly that many spots so the confirmed total stays stable.
        target_capacity = get_current_attendance_count(event.event_name) + freed_spots

    while True:
        waitlist = get_waitlist(event.event_name)
        if (
            event.archived
            or event.is_over
            or not waitlist
            or (excluded_user_ids and waitlist[0].user_id in excluded_user_ids)
        ):
            break
        if not any(waitlist[0] is entry for entry in eligible_entries):
            break  # A suspended older batch cannot consume a newly demoted or rejoined registration.
        if eligible_registrations is not None and registration_key(waitlist[0]) not in eligible_registrations:
            break
        # Check if we should continue promoting
        # (if target_capacity is None there is no constraint: drain the whole waitlist)
        if target_capacity is not None:
            # Check if we're at target capacity
            current_count = get_current_attendance_count(event.event_name)
            if current_count >= target_capacity:
                break

            # Check if there's anyone on the waitlist
            waitlist = get_waitlist(event.event_name)
            if not waitlist:
                break

            # Check if the next person fits
            next_entry = waitlist[0]
            next_total_people = 1 + next_entry.extra_people
            remaining_capacity = target_capacity - current_count
            if next_total_people > remaining_capacity:
                # Next person doesn't fit, stop promoting
                break

        # Promote the next person
        promoted_response = promote_waitlist_response(event)
        if promoted_response is None:
            break
        promoted_entry = promoted_response
        promoted_user_ids.append(promoted_entry.user_id)
        await credit_promoted_attendance(client, event, promoted_response)

        # Assign event participant role
        if guild and event.role_id:
            await assign_event_role(guild, promoted_entry.user_id, event.role_id, event=event)

        # Notify the promoted user
        try:
            promoted_user = await client.fetch_user(promoted_entry.user_id)
            await send_signup_reply(
                promoted_user.send,
                event,
                promoted_response,
                "Attendance confirmed",
                build_promotion_message(event, promoted_response),
                promotion=True,
            )
            _log.info("Promoted user %s from waitlist for event '%s'.", promoted_entry.user_id, event.event_name)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
            _log.warning(
                "Could not notify promoted user %s for event '%s': %s",
                promoted_entry.user_id,
                event.event_name,
                e,
            )
            await notify_dm_failure(client, event, promoted_entry.user_id)

    return promoted_user_ids


# Discord's modal title cap is smaller than MAX_EVENT_NAME_LENGTH, so a valid
# event name can still be too long to display as the modal's title.
MODAL_TITLE_MAX_LENGTH = 45


def _build_modal_title(event_name: str) -> str:
    if len(event_name) <= MODAL_TITLE_MAX_LENGTH:
        return event_name
    return event_name[: MODAL_TITLE_MAX_LENGTH - 1] + "…"


# Discord never reports a dismissed modal, and each modal has a unique custom_id,
# so discord.py only drops an unsubmitted modal from its store when it times out.
SIGNUP_MODAL_TIMEOUT_SECONDS = 60 * 60


# Class to handle the modal for event attendance
class GatheringModal(ui.Modal):
    def __init__(
        self,
        *,
        event: Event,
        payment_method: str | None = None,
        timeout: float | None = SIGNUP_MODAL_TIMEOUT_SECONDS,
    ):
        # No fixed custom_id: discord.py tracks open modals by custom_id alone, so a
        # per-event id routes every user's submission to whichever modal was opened
        # last. The library default is random per instance.
        super().__init__(
            title=_build_modal_title(event.event_name),
            timeout=timeout,
        )
        self.event = event
        self.payment_method = payment_method
        self.no_show_agreed = False
        self._submitted_answers: tuple[str, str, str, str, str] | None = None
        self.origin_context: SignupContext | None = None
        self.payment_deferred = False
        self.reply_in_dm = False
        self.fields = event.signup_form.get("fields", []) if event.signup_form else None

        self.preferred_name_input: ui.TextInput = ui.TextInput(
            label="Name you'd like us to use",
            placeholder="Optional; leave blank to use your Discord display name",
            required=False,
            max_length=32,
            custom_id="preferred_name",
        )
        self.extra_people_input: ui.TextInput = ui.TextInput(
            label="🧑 I am bringing extra people (0-5)",
            placeholder="Enter a number between 0-5",
            required=True,
            max_length=1,
            custom_id="extra_people",
        )
        self.confirmation_input: ui.TextInput = ui.TextInput(
            label="I agree to behave and arrive on time",
            placeholder=(
                "Type Yes to confirm both. After submitting, check your DMs for payment."
                if _payment_methods(event)
                else "Type Yes to confirm both"
            ),
            required=True,
            custom_id="behavior_arrival_confirm",
        )
        # These aliases keep older integrations from failing while the modal
        # itself uses one combined confirmation field.
        self.behave_checkbox_input: ui.TextInput = self.confirmation_input
        self.arrival_checkbox_input: ui.TextInput = self.confirmation_input

        if self.fields is None or "preferred_name" in self.fields:
            self.add_item(self.preferred_name_input)
        if self.fields is None or "guests" in self.fields:
            self.add_item(self.extra_people_input)
        self.add_item(self.confirmation_input)

        # Dynamically add drink choice only if needed
        self.drink_choice_input: ui.TextInput | None = None
        if self.event.has_drinks and (self.fields is None or "drinks" in self.fields):
            self.drink_choice_input = ui.TextInput(
                label="🍺 Drink choice(s) for you",  # Show available drinks
                placeholder=f"Choose from: {', '.join(self.event.drinks)}. Separate with commas.",
                required=True,
                custom_id="drink_choice",
            )
            self.add_item(self.drink_choice_input)

        self.extras_names_input: ui.TextInput = ui.TextInput(
            label="👥 Extras names",  # Show available drinks
            placeholder="Enter you extras names. Separate with commas.",
            required=False,
            max_length=160 if self.fields is not None else None,
            custom_id="extras_names",
        )
        if self.fields is None or "guests" in self.fields:
            self.add_item(self.extras_names_input)

    @property
    def event_name(self) -> str:
        return self.event.event_name

    # --- Validation Helpers (Raising Exceptions) ---

    def _validate_extra_people(self, extra_people_str: str) -> int:
        """Validates the extra people input.

        Returns:
            int specifying the number of extra people.

        Raises:
            ValidationError: If input is invalid.
        """
        return validate_extra_people_input(extra_people_str)

    def _validate_confirmations(self, confirmation_str: str, arrival_str: str | None = None) -> None:
        """Validate the combined acknowledgement, accepting case and padding variations."""

        confirmations = (confirmation_str,) if arrival_str is None else (confirmation_str, arrival_str)
        if not all(isinstance(value, str) and value.strip().casefold() == "yes" for value in confirmations):
            raise ValidationError("Please confirm behavior and arrival by typing 'Yes'.")

    def _validate_drinks(self, drink_choice_str: str, total_people: int) -> list[str]:
        """Validates the drink input based on event settings and total people.

        Returns:
            List of validated drink choices (lowercase).

        Raises:
            ValidationError: If drink input is invalid.
        """
        selected_drinks: list[str] = []
        if self.event.has_drinks and (self.fields is None or "drinks" in self.fields):
            if not drink_choice_str:
                raise ValidationError("Please specify your drink choice(s).")

            # Case-Insensitive Drink Validation
            raw_drinks_input = [drink.lower().strip() for drink in drink_choice_str.split(",")]
            raw_drinks_input = [d for d in raw_drinks_input if d]  # Filter out empty strings

            allowed_drinks_lower = [d.lower() for d in self.event.drinks]
            invalid_drinks = [d for d in raw_drinks_input if d not in allowed_drinks_lower]
            if invalid_drinks:
                raise ValidationError(
                    f"Invalid drink choices: {', '.join(invalid_drinks)}. Choose from: {', '.join(self.event.drinks)}"
                )

            # Check count matches total people
            if len(raw_drinks_input) != total_people:
                raise ValidationError(
                    f"Please provide exactly {total_people} drink choice(s) "
                    "(one for you and each extra person), separated by commas."
                )
            selected_drinks = raw_drinks_input
        else:
            # If drinks aren't needed, allow empty or "N/A"
            if drink_choice_str and drink_choice_str.lower() != "n/a":
                raise ValidationError(
                    "Drinks are not required for this event. Please enter 'N/A' or leave the drink field blank."
                )
            selected_drinks = []  # Ensure it's an empty list

        return selected_drinks

    def _validate_extra_people_names(self, extras: str, num_extra: int) -> list[str]:
        if num_extra == 0:
            if extras.strip() != "":
                raise ValidationError("You specified 0 extra people, so please leave the extras names field empty.")
            return []

        # If they bring extra people, extras names are required
        if not extras.strip():
            raise ValidationError(
                f"Please provide exactly {num_extra} name(s) "
                "(one for each person you are bringing), separated by commas."
            )

        names = [name.strip() for name in extras.split(",")]
        # Filter out empty names to ensure they don't submit ",,," or similar
        non_empty_names = [name for name in names if name]

        if len(non_empty_names) != num_extra:
            raise ValidationError(
                f"Please provide exactly {num_extra} non-empty name(s) "
                "(one for each person you are bringing), separated by commas."
            )
        return non_empty_names

    def _context(self, interaction: discord.Interaction) -> SignupContext:
        return self.origin_context or SignupContext.from_interaction(interaction)

    async def _deliver_signup_reply(
        self,
        interaction: discord.Interaction,
        entry: Response | WaitlistEntry,
        status: str,
        message: str,
        ack_text: str,
    ) -> None:
        """A delivery failure must not interrupt an already-saved registration."""
        try:
            if self.reply_in_dm:
                try:
                    send = (
                        interaction.followup.send
                        if interaction.response.is_done()
                        else interaction.response.send_message
                    )
                    await send_signup_reply(send, self.event, entry, status, message)
                except (discord.HTTPException, discord.InteractionResponded, ClientError, TimeoutError):
                    await send_signup_reply(interaction.user.send, self.event, entry, status, message)
            else:
                try:
                    await send_signup_reply(interaction.user.send, self.event, entry, status, message)
                except (discord.HTTPException, ClientError, TimeoutError):
                    await send_signup_reply(
                        interaction.response.send_message, self.event, entry, status, message, ephemeral=True
                    )
                else:
                    await interaction.response.send_message(ack_text, ephemeral=True)
        except (discord.HTTPException, ClientError, TimeoutError):
            _log.warning(
                "Registration saved for user %s in event %r, but the signup reply could not be delivered",
                entry.user_id,
                self.event_name,
            )

    async def _handle_successful_submission(self, interaction: discord.Interaction, response: Response):
        """Handles actions after a response is successfully added."""
        context = self._context(interaction)
        recorded_name = get_effective_display_name(response)

        await self._deliver_signup_reply(
            interaction,
            response,
            "Attendance confirmed",
            build_confirmation_message(self.event, response),
            f"✅ Your attendance is confirmed as **{recorded_name}**! I've sent you a DM with the details.",
        )

        # Membership must not depend on the optional ranking file or milestone update.
        await _add_user_to_event_thread(self.event, context, interaction.client)
        if self.event.role_id and context.guild:
            await assign_event_role(context.guild, context.user.id, self.event.role_id, event=self.event)

        # Update rank and announce milestones regardless of whether the DM succeeded
        # The signup is already saved; a ranking failure must not report the registration as failed.
        try:
            update_rank(context.user.id, context.user.name)
            await announce_rank_milestone(context.channel, context.user.id, context.user.name)
        except Exception:
            _log.exception("Rank update failed after signup by %s for %r", context.user.id, self.event.event_name)

    async def _handle_waitlist_submission(self, interaction: discord.Interaction, entry: WaitlistEntry):
        """Handles actions after a user is added to the waitlist."""
        context = self._context(interaction)
        recorded_name = get_effective_display_name(entry)

        await self._deliver_signup_reply(
            interaction,
            entry,
            "Waitlisted — attendance is not confirmed",
            build_waitlist_message(self.event, entry),
            f"📋 You've been added to the waitlist as **{recorded_name}**! I've sent you a DM with the details.",
        )

        await _add_user_to_event_thread(self.event, context, interaction.client)

    async def _handle_waitlist_capacity_exceeded(
        self, interaction: discord.Interaction, entry: WaitlistEntry, total_people_in_group: int, remaining_spots: int
    ):
        """Handles actions when a user's group exceeds capacity and is added to waitlist."""
        context = self._context(interaction)
        recorded_name = get_effective_display_name(entry)
        # 1. Create the capacity exceeded + waitlist message
        drinks_msg = f"\n🍺 Drinks: {', '.join(entry.drinks)}" if entry.drinks else ""
        drinks_msg_jp = f"\n🍺 飲み物: {', '.join(entry.drinks)}" if entry.drinks else ""
        waitlist_message = (
            f"❌ Sorry, your group of {total_people_in_group} people would exceed the capacity "
            f"for **{self.event.event_name}**.\n"
            f"Only {remaining_spots} spot(s) remaining out of {self.event.max_capacity} total.\n\n"
            f"📋 For now you will be added to the waiting list.\n"
            f"👤 Name recorded: {recorded_name}\n"
            f"👥 Bringing: {entry.extra_people} extra guest(s)\n"
            f"✔ Behavior Confirmed\n"
            f"✔ Arrival Confirmed"
            f"{drinks_msg}\n\n"
            f"❌ 申し訳ありませんが、{total_people_in_group}名のグループは"
            f"**{self.event.event_name}**の定員を超えてしまいます。\n"
            f"定員{self.event.max_capacity}名中、残り{remaining_spots}名分です。\n\n"
            f"📋 現在ウェイトリストに追加されています。\n"
            f"👤 登録名: {recorded_name}\n"
            f"👥 同伴者: {entry.extra_people}名\n"
            f"✔ 行動確認済み\n"
            f"✔ 到着確認済み"
            f"{drinks_msg_jp}\n\n"
            f"You can choose to leave the offkai and re-apply with fewer people, "
            f"or stay on the waitlist and be automatically added if a spot opens up.\n"
            f"人数を減らして再申請するか、ウェイトリストに残って空きが出た場合に"
            f"自動的に追加されるのをお待ちいただけます。\n\n"
            f"💰 **Note:** If no one drops out and you are still allowed to join the offkai, "
            f"you may be charged extra by the organizers.\n"
            f"💰 **注意:** 誰もキャンセルせず、それでもオフ会への参加が認められた場合、"
            f"主催者から追加料金が請求される場合があります。"
        )

        await self._deliver_signup_reply(
            interaction,
            entry,
            "Waitlisted — attendance is not confirmed",
            waitlist_message,
            "📋 Your group exceeds capacity. You've been added to the waitlist! "
            f"Your name is recorded as **{recorded_name}**. I've sent you a DM with the details.",
        )

        await _add_user_to_event_thread(self.event, context, interaction.client)

    async def _send_capacity_reached_message(self, interaction: discord.Interaction):
        """Sends a message to the thread when capacity is first reached."""
        context = self._context(interaction)
        try:
            if isinstance(context.channel, discord.Thread):
                await context.channel.send(
                    f"⚠️ **Maximum capacity has been reached for {self.event.event_name}!**\n"
                    f"New registrations will be added to the waitlist.\n\n"
                    f"⚠️ **{self.event.event_name}の定員に達しました！**\n"
                    f"新規登録はウェイトリストに追加されます。"
                )
                _log.info("Sent capacity reached message to thread for event '%s'.", self.event.event_name)
            else:
                _log.warning("Could not send capacity message to thread %s (not a thread?).", context.channel_id)
        except discord.HTTPException as e:
            _log.error("Failed to send capacity message to thread %s: %s", context.channel_id, e)

    async def on_submit(self, interaction: discord.Interaction):
        await self._submit(interaction)

    async def _submit(self, interaction: discord.Interaction, *, final: bool = False):
        if self.event.archived:
            await modal_error_message(
                interaction,
                self.event_name,
                "This event is archived and no longer accepts signups.",
                in_dm=self.reply_in_dm,
            )
            return False

        # 1. Get Input Values. The final (post-payment) step reuses the answers
        # captured on the first submit rather than re-reading the input widgets.
        if final and self._submitted_answers is not None:
            answers = self._submitted_answers
        else:
            answers = (
                self.preferred_name_input.value,
                self.extra_people_input.value if self.fields is None or "guests" in self.fields else "0",
                self.confirmation_input.value,
                self.drink_choice_input.value if self.drink_choice_input else "N/A",
                self.extras_names_input.value if self.fields is None or "guests" in self.fields else "",
            )
        preferred_name_str, extra_people_str, confirmation_str, drink_choice_str, extra_names_str = answers
        try:
            methods = _payment_methods(self.event)
            # 2. Validate Inputs using Helpers (Raises ValidationError on failure)
            num_extra_people = self._validate_extra_people(extra_people_str)
            # Older callers may still replace the two compatibility aliases.
            # Prefer the combined field whenever it has a value.
            if not isinstance(confirmation_str, str) or not confirmation_str.strip():
                aliases_replaced = (
                    self.behave_checkbox_input is not self.confirmation_input
                    or self.arrival_checkbox_input is not self.confirmation_input
                )
                if aliases_replaced:
                    self._validate_confirmations(
                        self.behave_checkbox_input.value,
                        self.arrival_checkbox_input.value,
                    )
                else:
                    self._validate_confirmations(confirmation_str)
            else:
                self._validate_confirmations(confirmation_str)
            selected_drinks = self._validate_drinks(drink_choice_str, num_extra_people + 1)
            extra_people_names = self._validate_extra_people_names(extra_names_str, num_extra_people)

            if any(
                entry.user_id == interaction.user.id
                for entry in [*get_responses(self.event.event_name), *get_waitlist(self.event.event_name)]
            ):
                raise DuplicateResponseError(self.event.event_name, interaction.user.id)

            will_waitlist = (
                self.event.is_past_deadline
                or not self.event.open
                or would_exceed_capacity(self.event, 1 + num_extra_people)
            )
            if not final:
                self.payment_deferred = will_waitlist
            selection_methods = {} if self.payment_deferred or will_waitlist else methods

            if selection_methods or (self.fields and "no_show" in self.fields):
                if not final:
                    self._submitted_answers = answers
                    prompt = "Please confirm the no-show / no-refund agreement to complete your registration."
                    self.origin_context = SignupContext.from_interaction(interaction)
                    view = SignupContinuation(self, interaction.user.id)
                    if methods:
                        if selection_methods:
                            prompt = (
                                "Please choose a payment method to complete your registration. "
                                "I'll send you the payment instructions after you choose.\n\n"
                                "This button expires in 5 minutes. If it no longer works, restart registration "
                                "using the event's signup button."
                            )
                        else:
                            prompt = (
                                "Please confirm the no-show / no-refund agreement to join the waitlist. "
                                "You do not need to choose a payment method or pay while waitlisted. "
                                "If promoted, you will get 72 hours to choose a payment method."
                            )
                        self.reply_in_dm = True
                        await interaction.response.defer(ephemeral=True, thinking=True)
                        try:
                            event_name = discord.utils.escape_markdown(self.event_name)
                            view.message = await interaction.user.send(f"**{event_name}**\n\n{prompt}", view=view)
                        except (discord.Forbidden, discord.HTTPException):
                            view.stop()
                            self.reply_in_dm = False
                            await interaction.followup.send(
                                DM_FAILURE_NOTICE + "click "
                                "the event's signup button again to restart registration. "
                                "You haven't been registered yet.",
                                ephemeral=True,
                            )
                        else:
                            await view.activate()
                            notice = (
                                "This payment prompt is closed. Check your DMs for your registration status."
                                if view.finished
                                else (
                                    "Check your DMs now. Confirm the agreement there to join the waitlist."
                                    if self.payment_deferred
                                    else "Check your DMs now. Choose a payment method there "
                                    "to complete your registration."
                                )
                            )
                            try:
                                await interaction.followup.send(notice, ephemeral=True)
                            except discord.HTTPException:
                                _log.warning(
                                    "Payment DM sent, but could not deliver notice for user %s", interaction.user.id
                                )
                    else:
                        await interaction.response.send_message(prompt, view=view, ephemeral=True)
                    return
                if selection_methods and self.payment_method not in methods:
                    raise ValidationError("Please choose an enabled payment method before signing up.")
                if self.fields and "no_show" in self.fields and not self.no_show_agreed:
                    raise ValidationError("Please agree to the no-show / no-refund policy by typing 'Yes'.")

            # 3. Calculate total people in this registration
            total_people_in_group = 1 + num_extra_people
            signup_user = self._context(interaction).user
            username = signup_user.name
            resolved_display_name = resolve_submitted_display_name(
                preferred_name_str,
                getattr(signup_user, "display_name", ""),
                username,
            )

            # 4. Check if event has reached capacity, if deadline has passed, or if event is closed
            is_past_deadline = self.event.is_past_deadline
            is_closed = not self.event.open
            at_capacity = is_event_at_capacity(self.event)

            # 5. Determine whether to add to responses or waitlist
            # If deadline has passed OR event is closed OR event is at capacity, add to waitlist
            if is_past_deadline or is_closed or at_capacity:
                # Add to waitlist
                new_entry = WaitlistEntry(
                    user_id=interaction.user.id,
                    username=interaction.user.name,
                    extra_people=num_extra_people,
                    behavior_confirmed=True,
                    arrival_confirmed=True,
                    event_name=self.event.event_name,
                    timestamp=datetime.now(UTC),
                    drinks=selected_drinks,
                    extras_names=extra_people_names,
                    display_name=resolved_display_name,
                    payment_method=None,
                    no_show_agreed=self.no_show_agreed,
                    payment_selection_demotes=bool(methods),
                )

                add_to_waitlist(self.event.event_name, new_entry)

                # Send waitlist confirmation
                await self._handle_waitlist_submission(interaction, new_entry)

            elif would_exceed_capacity(self.event, total_people_in_group):
                # Registration would exceed capacity - add to waitlist with special message
                remaining = get_remaining_capacity(self.event)
                # would_exceed_capacity only returns True when there's a capacity limit
                assert remaining is not None, "Capacity should be set if would_exceed_capacity is True"

                # Create waitlist entry
                new_entry = WaitlistEntry(
                    user_id=interaction.user.id,
                    username=interaction.user.name,
                    extra_people=num_extra_people,
                    behavior_confirmed=True,
                    arrival_confirmed=True,
                    event_name=self.event.event_name,
                    timestamp=datetime.now(UTC),
                    drinks=selected_drinks,
                    extras_names=extra_people_names,
                    display_name=resolved_display_name,
                    payment_method=None,
                    no_show_agreed=self.no_show_agreed,
                    payment_selection_demotes=bool(methods),
                )

                # Add to waitlist
                add_to_waitlist(self.event.event_name, new_entry)

                # Send capacity exceeded + waitlist confirmation
                await self._handle_waitlist_capacity_exceeded(interaction, new_entry, total_people_in_group, remaining)

            else:
                # Event is not at capacity and deadline hasn't passed - add to regular responses
                new_response = Response(
                    user_id=interaction.user.id,
                    username=interaction.user.name,
                    extra_people=num_extra_people,
                    behavior_confirmed=True,
                    arrival_confirmed=True,
                    event_name=self.event.event_name,
                    timestamp=datetime.now(UTC),
                    drinks=selected_drinks,
                    extras_names=extra_people_names,
                    display_name=resolved_display_name,
                    payment_method=self.payment_method,
                    payment_selection_expires_at=(
                        datetime.now(UTC) + timedelta(hours=72) if methods and self.payment_deferred else None
                    ),
                    payment_selection_demotes=bool(methods and self.payment_deferred),
                    no_show_agreed=self.no_show_agreed,
                )

                add_response(self.event.event_name, new_response)
                await self._handle_successful_submission(interaction, new_response)

                # Check if we just reached capacity
                if self.event.max_capacity is not None and not is_past_deadline and not is_closed:
                    current_count = get_current_attendance_count(self.event.event_name)
                    if current_count == self.event.max_capacity:
                        await self._send_capacity_reached_message(interaction)
            return True

        except ValidationError as e:
            # Handle specific validation errors raised by helpers
            _log.info(
                "Rejected modal submission for event '%s' by user %s: %s",
                self.event.event_name,
                interaction.user.id,
                e,
            )
            await modal_error_message(interaction, self.event.event_name, str(e), in_dm=self.reply_in_dm)
            # No return needed here, function ends after except block

        except DuplicateResponseError as e:
            await modal_error_message(interaction, self.event.event_name, str(e), in_dm=self.reply_in_dm)

        except Exception as e:
            # Catch any other unexpected errors during Response creation or add_response
            _log.error("Unexpected error during modal submission for %s: %s", self.event.event_name, e, exc_info=True)
            await error_message(interaction, "An internal error occurred processing your response.")
            # No return needed here


class InterestCheckModal(ui.Modal):
    """Minimal modal for interest checks: just the group size, no commitments."""

    def __init__(
        self,
        *,
        event: Event,
        timeout: float | None = SIGNUP_MODAL_TIMEOUT_SECONDS,
    ):
        # No fixed custom_id, for the same reason as GatheringModal.
        super().__init__(
            title=_build_modal_title(event.event_name),
            timeout=timeout,
        )
        self.event = event

        self.preferred_name_input: ui.TextInput = ui.TextInput(
            label="Name you'd like us to use",
            placeholder="Optional; leave blank to use your Discord display name",
            required=False,
            max_length=32,
            custom_id="preferred_name",
        )
        self.extra_people_input: ui.TextInput = ui.TextInput(
            label="🧑 Extra people you'd bring (0-5)",
            placeholder="Enter a number between 0-5",
            required=True,
            max_length=1,
            custom_id="interest_extra_people",
        )
        self.add_item(self.preferred_name_input)
        self.add_item(self.extra_people_input)

    @property
    def event_name(self) -> str:
        return self.event.event_name

    async def on_submit(self, interaction: discord.Interaction):
        try:
            # The modal can remain open in a user's client after the interest
            # check has been closed, so re-check its state at submission time.
            if self.event.archived or not self.event.open or self.event.is_past_deadline:
                await error_message(interaction, "This interest check is no longer accepting responses.")
                return

            num_extra_people = validate_extra_people_input(self.extra_people_input.value)
            resolved_display_name = resolve_submitted_display_name(
                self.preferred_name_input.value,
                getattr(interaction.user, "display_name", ""),
                interaction.user.name,
            )

            new_response = Response(
                user_id=interaction.user.id,
                username=interaction.user.name,
                extra_people=num_extra_people,
                behavior_confirmed=False,
                arrival_confirmed=False,
                event_name=self.event.event_name,
                timestamp=datetime.now(UTC),
                drinks=[],
                extras_names=[],
                display_name=resolved_display_name,
            )
            add_response(self.event.event_name, new_response)

            # Interest is non-binding: no DM, no rank/milestone update, no role.
            group_size = 1 + num_extra_people
            try:
                await interaction.response.send_message(
                    f"🙋 Thanks! You're counted as interested in **{self.event.event_name}** "
                    f"as **{resolved_display_name}** "
                    f"({group_size} {'person' if group_size == 1 else 'people'}).\n"
                    f"🙋 ありがとうございます！**{self.event.event_name}**に"
                    f"{resolved_display_name}さんとして"
                    f"興味あり（{group_size}名）として記録されました。",
                    ephemeral=True,
                )
            except (discord.HTTPException, discord.InteractionResponded, ClientError, TimeoutError) as error:
                _log.warning("Interest saved but acknowledgement failed for user %s: %s", interaction.user.id, error)

            # Add user to the thread so they can be reached for follow-ups.
            try:
                await _add_user_to_event_thread(
                    self.event, SignupContext.from_interaction(interaction), interaction.client
                )
            except (discord.HTTPException, ClientError, TimeoutError) as error:
                _log.warning("Interest saved but thread join failed for user %s: %s", interaction.user.id, error)

            await _refresh_interest_check_message(interaction.client, self.event)

        except ValidationError as e:
            await error_message(interaction, str(e))

        except DuplicateResponseError as e:
            # The error message already carries the ❌ prefix.
            await interaction.response.send_message(str(e), ephemeral=True)

        except Exception as e:
            _log.error(
                "Unexpected error during interest check submission for %s: %s",
                self.event.event_name,
                e,
                exc_info=True,
            )
            await error_message(interaction, "An internal error occurred processing your response.")


async def _refresh_interest_check_message(client: discord.Client, event: Event) -> None:
    """Re-renders the interest check announcement so the live count stays current."""
    # Imported locally: event_actions imports this module at import time.
    from offkai_bot.event_actions import update_event_message

    try:
        await update_event_message(client, event)
    except Exception as e:
        # The response is already recorded; a stale count is not worth failing the interaction.
        _log.error("Failed to refresh interest check message for '%s': %s", event.event_name, e, exc_info=True)


async def withdraw_registration(event: Event, interaction: discord.Interaction, *, late_reason: str | None = None):
    """Withdraw the clicking user from an event's attendees or waitlist.

    The removal is saved first; every later step is attempted independently, so a failed
    DM or interaction reply can never skip role/thread cleanup or waitlist promotion.
    ``late_reason`` (e.g. "after the deadline") adds the late-withdrawal warning and
    notifies the event creator.
    """
    user = interaction.user
    if event.archived:
        await error_message(interaction, f"**{event.event_name}** is archived, so registrations can no longer change.")
        return

    removed_response: Response | None = None
    try:
        try:
            removed_response = remove_response(event.event_name, user.id)
        except ResponseNotFoundError:
            remove_from_waitlist(event.event_name, user.id)
    except ResponseNotFoundError:
        await error_message(interaction, f"You have not registered for **{event.event_name}**, so you cannot withdraw.")
        return
    except Exception as e:
        _log.error("Unexpected error during withdrawal for %s by %s: %s", event.event_name, user.id, e, exc_info=True)
        await error_message(interaction, "An internal error occurred while processing your withdrawal.")
        return

    async def attempt(step: str, action: Callable[[], Awaitable[Any]]) -> None:
        try:
            await action()
        except Exception as e:
            _log.error(
                "Withdrawal step '%s' failed for %s by %s: %s", step, event.event_name, user.id, e, exc_info=True
            )

    if removed_response is not None:
        try:
            decrease_rank(user.id, user.name)
        except Exception:
            _log.exception("Rank update failed after withdrawal by %s from %r", user.id, event.event_name)

    # 1. Tell the user first: the interaction must be answered promptly.
    if late_reason:
        withdrawal_message = (
            f"👋 Your attendance for **{event.event_name}** has been withdrawn.\n\n"
            f"⚠️ **Important:** Withdrawing {late_reason} is your full responsibility. "
            f"You may be contacted by the event organizer for payment if needed. "
            f"Failure to comply may result in server moderation action.\n\n"
            f"👋 **{event.event_name}**への参加が取り消されました。\n\n"
            f"⚠️ **重要:** 締め切り後の辞退はご自身の全責任となります。"
            f"主催者から支払いについて連絡が来る場合があります。"
            f"従わない場合、サーバーのモデレーション措置が取られる可能性があります。"
        )
    else:
        withdrawal_message = (
            f"👋 Your attendance for **{event.event_name}** has been withdrawn.\n"
            f"👋 **{event.event_name}**への参加が取り消されました。"
        )

    async def notify_user() -> None:
        try:
            await user.send(withdrawal_message)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(withdrawal_message, ephemeral=True)
        else:
            await interaction.response.send_message(
                "✅ Your withdrawal is confirmed. I've sent you a DM.", ephemeral=True
            )

    await attempt("notify user", notify_user)

    # 2. Required cleanup, independent of whether the user could be told.
    async def leave_thread() -> None:
        if isinstance(interaction.channel, discord.Thread):
            await remove_event_thread_member(event, interaction.channel, user)
        else:
            _log.warning("Could not remove user %s from channel %s (not a thread?).", user.id, interaction.channel_id)

    await attempt("leave thread", leave_thread)
    if removed_response is not None:
        guild = interaction.guild
        role_id = event.role_id
        if role_id and guild:
            await attempt("remove role", lambda: remove_event_role(guild, user.id, role_id, event=event))
        # Only an attendee withdrawal frees capacity.
        freed_spots = 1 + removed_response.extra_people
        await attempt(
            "promote waitlist", lambda: promote_waitlist_batch(event, interaction.client, freed_spots=freed_spots)
        )

    # 3. Organizer notice for late withdrawals.
    creator_id = event.creator_id
    if late_reason and creator_id:

        async def notify_creator() -> None:
            creator = await interaction.client.fetch_user(creator_id)
            await creator.send(
                f"⚠️ **Withdrawal Notification**\n\n"
                f"User {user.mention} ({user.name}) has withdrawn from **{event.event_name}**.\n"
                f"This withdrawal occurred {late_reason}."
            )

        await attempt("notify creator", notify_creator)


# --- Views ---
class EventView(ui.View):
    def __init__(self, event: Event):  # Expect Event object
        super().__init__(timeout=None)
        self.event = event  # Store the Event object

    @discord.ui.button(
        label="Attendance Count",
        style=discord.ButtonStyle.secondary,
        row=2,
        custom_id="count_button",  # Use secondary style
    )
    async def count(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Use get_responses from util
        event_responses = get_responses(self.event.event_name)

        num = sum(1 + response.extra_people for response in event_responses)

        await interaction.response.send_message(
            f"📝 Current registration count for **{self.event.event_name}**: {num}",
            ephemeral=True,
        )


class ArchivedEvent(EventView):
    """Archived events retain only the read-only attendance count."""


class OpenEvent(EventView):
    def __init__(self, event: Event):  # Expect Event object
        super().__init__(event=event)  # Pass event to parent

    @discord.ui.button(
        label="Confirm Attendance",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="confirm_button",  # Use success style
    )
    async def respond(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Pass the Event object to the modal
        await start_signup(interaction, self.event)

    @discord.ui.button(
        label="Withdraw Attendance",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="withdraw_button",  # Use danger style
    )
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await withdraw_registration(self.event, interaction)


class ClosedEvent(EventView):
    def __init__(self, event: Event):  # Expect Event object
        super().__init__(event=event)  # Pass event to parent

    @discord.ui.button(
        label="Responses Closed",
        style=discord.ButtonStyle.secondary,
        disabled=True,
        row=0,
        custom_id="closed_button",
    )
    async def respond(self, interaction: discord.Interaction, button: discord.ui.Button):
        # This button is disabled, so this callback shouldn't trigger
        # If it somehow does, send an ephemeral message
        await interaction.response.send_message("Responses are currently closed for this event.", ephemeral=True)

    @discord.ui.button(
        label="Join Waitlist",
        style=discord.ButtonStyle.primary,
        row=1,
        custom_id="join_waitlist_closed_button",
    )
    async def join_waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Show the modal to join waitlist
        await start_signup(interaction, self.event)

    @discord.ui.button(
        label="Withdraw Attendance",
        style=discord.ButtonStyle.danger,
        row=2,
        custom_id="withdraw_button_closed",
    )
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await withdraw_registration(self.event, interaction, late_reason="after responses are closed")


class InterestCheckEvent(EventView):
    """View for open interest checks: non-binding register/withdraw only."""

    def __init__(self, event: Event):
        super().__init__(event=event)

    @discord.ui.button(
        label="🙋 I'm interested",
        style=discord.ButtonStyle.success,
        row=0,
        custom_id="interest_button",
    )
    async def register_interest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.event.archived:
            await error_message(interaction, "This interest check is archived and no longer accepts responses.")
            return
        await interaction.response.send_modal(InterestCheckModal(event=self.event))

    @discord.ui.button(
        label="Withdraw interest",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="interest_withdraw_button",
    )
    async def withdraw_interest(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.event.archived:
            await error_message(interaction, "This interest check is archived and cannot be changed.")
            return
        try:
            remove_response(self.event.event_name, interaction.user.id)
        except ResponseNotFoundError:
            await error_message(
                interaction,
                f"You have not registered interest in **{self.event.event_name}**.",
            )
            return
        except Exception as e:
            _log.error(
                "Unexpected error during interest withdrawal for %s by %s: %s",
                self.event.event_name,
                interaction.user.id,
                e,
                exc_info=True,
            )
            await error_message(interaction, "An internal error occurred while processing your withdrawal.")
            return

        # Interest is non-binding: no warning DMs, no rank change, no waitlist promotion.
        await interaction.response.send_message(
            f"👋 Your interest in **{self.event.event_name}** has been withdrawn.\n"
            f"👋 **{self.event.event_name}**への興味ありが取り消されました。",
            ephemeral=True,
        )

        try:
            if interaction.channel and isinstance(interaction.channel, discord.Thread):
                await remove_event_thread_member(self.event, interaction.channel, interaction.user)
        except discord.HTTPException as e:
            _log.error(
                "Failed to remove user %s from thread %s: %s",
                interaction.user.id,
                interaction.channel_id,
                e,
            )

        await _refresh_interest_check_message(interaction.client, self.event)


class InterestCheckClosedEvent(EventView):
    """View for closed interest checks: the tally is a snapshot, no more input."""

    def __init__(self, event: Event):
        super().__init__(event=event)

    @discord.ui.button(
        label="Interest check closed",
        style=discord.ButtonStyle.secondary,
        disabled=True,
        row=0,
        custom_id="interest_closed_button",
    )
    async def closed(self, interaction: discord.Interaction, button: discord.ui.Button):
        # This button is disabled, so this callback shouldn't trigger
        await interaction.response.send_message("This interest check is closed.", ephemeral=True)


class PostDeadlineEvent(EventView):
    """View shown after the deadline has passed - allows joining the waitlist only."""

    def __init__(self, event: Event):  # Expect Event object
        super().__init__(event=event)  # Pass event to parent

    @discord.ui.button(
        label="Join Waitlist",
        style=discord.ButtonStyle.primary,
        row=0,
        custom_id="join_waitlist_button",
    )
    async def join_waitlist(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Show the same modal, but it will add to waitlist since deadline has passed
        await start_signup(interaction, self.event)

    @discord.ui.button(
        label="Withdraw Attendance",
        style=discord.ButtonStyle.danger,
        row=1,
        custom_id="withdraw_button_deadline",
    )
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await withdraw_registration(self.event, interaction, late_reason="after the deadline")


def render_custom_reply_sections(event: Event, entry: Response | WaitlistEntry, status: str) -> list[str]:
    """Complete language sections; organizer translations are optional and never inferred."""
    methods = (event.signup_form or {}).get("payment_methods", {})
    instruction = methods.get(entry.payment_method, "")
    jp_instruction = (event.signup_form or {}).get("payment_instructions_jp", {}).get(entry.payment_method, "")
    confirmed = isinstance(entry, Response) and status == "Attendance confirmed"
    has_payment = confirmed and entry.payment_method in methods
    name = get_effective_display_name(entry)
    guests = ", ".join(entry.extras_names)
    drinks = entry.drinks if "drinks" in (event.signup_form or {}).get("fields", []) else []
    lines = [
        f"✅ Attendance confirmed for **{event.event_name}**!"
        if confirmed
        else f"📋 Waitlisted — attendance is not confirmed: **{event.event_name}**",
        f"👤 Name recorded: {name}",
        f"👥 Bringing: {entry.extra_people} extra guest(s)" + (f" ({guests})" if guests else ""),
        "✔ Behavior Confirmed\n✔ Arrival Confirmed",
    ]
    if drinks:
        lines.append(f"🍺 Drinks: {', '.join(drinks)}")
    if has_payment:
        lines.extend([f"💳 Payment method: {entry.payment_method}", f"Payment instructions: {instruction}"])
    elif not confirmed and methods:
        lines.append("Do not pay while waitlisted. If promoted, you will get 72 hours to choose a payment method.")
    if entry.no_show_agreed:
        lines.append("✔ No-show / no-refund policy agreed: payments for no-shows are not refunded.")
    url = build_checkin_url(entry.user_id, event.event_name)
    proof_command = discord.utils.escape_markdown(f"I paid {event.event_name}")
    if url:
        if has_payment:
            lines.append(
                f'📎 **Please provide proof of payment by replying with "{proof_command}" '
                "and a screenshot in the same message, or upload it to the RSVP page.**"
            )
        lines.append(f"🔗 RSVP Page / QR Code: {url}")
    lines.extend(
        [
            "",
            "⚠️ Important: Withdrawing after the deadline is strongly discouraged. "
            "If you withdraw late, you are fully responsible for any consequences, including "
            "payment requests from the event organizer and potential server moderation action.",
        ]
    )
    jp_lines = [
        f"✅ 参加確定: **{event.event_name}**"
        if confirmed
        else f"📋 ウェイトリスト（参加未確定）: **{event.event_name}**",
        f"👤 登録名: {name}",
        f"👥 同伴者: {entry.extra_people}名" + (f" ({guests})" if guests else ""),
        "✔ 行動確認済み\n✔ 到着確認済み",
    ]
    if drinks:
        jp_lines.append(f"🍺 飲み物: {', '.join(drinks)}")
    if has_payment:
        jp_lines.append(f"💳 支払い方法: {entry.payment_method}")
    if has_payment and jp_instruction:
        jp_lines.append(f"支払い案内: {jp_instruction}")
    if not confirmed and methods:
        jp_lines.append("ウェイトリスト中は支払わないでください。参加確定後、72時間以内に支払い方法を選択できます。")
    if entry.no_show_agreed:
        jp_lines.append("✔ 不参加時の返金不可に同意済み")
    if url:
        if has_payment:
            jp_lines.append(
                f"📎 **「{proof_command}」と支払い証明のスクリーンショットを同じメッセージで返信するか、"
                "RSVPページにアップロードしてください。**"
            )
        jp_lines.append(f"🔗 RSVPページ / QRコード: {url}")
    jp_lines.extend(
        [
            "",
            "⚠️ 重要: 締め切り後の辞退は強くお勧めしません。遅れて辞退した場合、"
            "主催者からの支払い請求やサーバーのモデレーション措置を含む"
            "すべての結果に対して、全責任を負います。",
        ]
    )
    return ["\n".join(lines), "\n".join(jp_lines)]


def render_custom_reply(event: Event, entry: Response | WaitlistEntry, status: str) -> str:
    return "\n\n".join(render_custom_reply_sections(event, entry, status))


def custom_reply_embeds(event: Event, entry: Response | WaitlistEntry, status: str) -> list[discord.Embed]:
    return [discord.Embed(description=section) for section in render_custom_reply_sections(event, entry, status)]


async def send_signup_reply(
    send: Callable[..., Awaitable[Any]],
    event: Event,
    entry: Response | WaitlistEntry,
    status: str,
    default_message: str,
    promotion: bool = False,
    notice: tuple[str, str] | None = None,
    **kwargs: Any,
) -> None:
    """`notice` is an (English, Japanese) pair shown above the reply."""

    def current_promotion() -> bool:
        if not promotion or not isinstance(entry, Response):
            return True
        current = next((r for r in get_responses(event.event_name) if r.user_id == entry.user_id), None)
        return (
            not event.archived
            and current is not None
            and current.timestamp == entry.timestamp
            and current.payment_selection_expires_at == entry.payment_selection_expires_at
            and current.payment_method == entry.payment_method
        )

    if not current_promotion():
        return  # Withdrawn, demoted, replaced or completed while Discord work was pending.

    async def deliver(*args: Any, **options: Any) -> None:
        message = await send(*args, **options)
        if not current_promotion():
            view = options.get("view")
            if view is not None:
                view.stop()
            if isinstance(message, discord.Message):
                try:
                    await message.delete()  # A sent request cannot be cancelled retroactively.
                except discord.HTTPException:
                    _log.warning("Could not remove stale promotion DM for user %s", entry.user_id)

    sections = render_custom_reply_sections(event, entry, status) if event.signup_form else []
    if sections and promotion:
        sections[0] = (
            "🎉 A spot has opened up! You have moved from the waitlist to confirmed attendance.\n\n" + sections[0]
        )
        sections[1] = "🎉 空きが出たため、ウェイトリストから参加確定に移動しました。\n\n" + sections[1]
    if sections and notice:
        sections[0] = f"{notice[0]}\n\n{sections[0]}"
        sections[1] = f"{notice[1]}\n\n{sections[1]}"
    elif notice:
        default_message = f"{notice[0]}\n{notice[1]}\n\n{default_message}"
    message = "\n\n".join(sections) if sections else default_message
    if isinstance(entry, Response) and entry.payment_selection_expires_at and not entry.payment_method:
        expiry = entry.payment_selection_expires_at
        if datetime.now(UTC) < expiry and not event.archived and _payment_methods(event):
            kwargs["view"] = PromotedPaymentSelection(event, entry)
            choice_notice = (
                f"Choose your payment method within 72 hours (by <t:{int(expiry.timestamp())}:F>). "
                "Payment instructions will be sent after you choose.\n"
                "参加確定から72時間以内に支払い方法を選択してください。選択後に支払い案内をお送りします。"
            )
            # Keep long existing language embeds within Discord's content cap.
            if sections:
                kwargs["embeds"] = [discord.Embed(description=section) for section in sections]
            try:
                await deliver(choice_notice if sections else f"{message}\n\n{choice_notice}", **kwargs)
            except Exception:
                kwargs["view"].stop()
                raise
            return
    if sections and len(message) > 2000:
        await deliver(embeds=[discord.Embed(description=section) for section in sections], **kwargs)
    else:
        await deliver(message, **kwargs)


async def start_signup(interaction: discord.Interaction, event: Event):
    if event.archived:
        await error_message(interaction, "This event is archived and no longer accepts signups.")
        return
    await interaction.response.send_modal(GatheringModal(event=event))


class PromotedPaymentSelection(ui.View):
    """Persistent choice bound to one confirmed registration and its original 72-hour deadline."""

    def __init__(self, event: Event, entry: Response):
        super().__init__(timeout=None)
        self.event = event
        self.owner_id = entry.user_id
        self.timestamp = entry.timestamp
        self.expires_at = entry.payment_selection_expires_at
        delay = max(0, (self.expires_at - datetime.now(UTC)).total_seconds()) if self.expires_at else 0
        self._expiry_handle = asyncio.get_running_loop().call_later(delay, self.stop)
        self.message: discord.Message | None = None
        identity = f"{event.event_name}:{entry.user_id}:{entry.timestamp.isoformat()}:{self.expires_at}"
        self.choose_payment.custom_id = f"offkai-payment:{hashlib.sha256(identity.encode()).hexdigest()[:40]}"

    def stop(self) -> None:
        self._expiry_handle.cancel()
        super().stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        entry = next((r for r in get_responses(self.event.event_name) if r.user_id == self.owner_id), None)
        if (
            interaction.user.id != self.owner_id
            or self.event.archived
            or not _payment_methods(self.event)
            or self.expires_at is None
            or datetime.now(UTC) >= self.expires_at
            or entry is None
            or entry.timestamp != self.timestamp
            or entry.payment_selection_expires_at != self.expires_at
            or entry.payment_method is not None
        ):
            await error_message(
                interaction,
                "This payment choice has expired, been completed, or belongs to another registration. "
                "Contact the organizer if you still need to choose or be promoted again.",
            )
            return False
        return True

    @ui.button(label="Choose Payment Method", style=discord.ButtonStyle.primary, custom_id="offkai-payment")
    async def choose_payment(self, interaction: discord.Interaction, button: ui.Button):
        # discord.py has already run interaction_check for this button dispatch.
        if isinstance(interaction.message, discord.Message):
            self.message = interaction.message
        await interaction.response.send_modal(PromotedPaymentModal(self))


class PromotedPaymentModal(ui.Modal):
    def __init__(self, selection: PromotedPaymentSelection):
        # Reopening this modal does not reset the persisted promotion deadline.
        super().__init__(title="Choose payment method", timeout=300)
        self.selection = selection
        self.payment_method_input = ui.Select(
            placeholder="Choose your payment method",
            custom_id="payment_method",
            required=True,
            options=[discord.SelectOption(label=name) for name in _payment_methods(selection.event)],
        )
        self.add_item(ui.Label(text="Payment method", component=self.payment_method_input))

    async def on_submit(self, interaction: discord.Interaction):
        selection = self.selection
        if not await selection.interaction_check(interaction):
            return
        selected = self.payment_method_input.values
        if len(selected) != 1 or selected[0] not in _payment_methods(selection.event):
            await error_message(interaction, "Please choose an enabled payment method.")
            return
        assert selection.expires_at is not None
        try:
            entry = complete_payment_selection(
                selection.event.event_name, selection.owner_id, selection.timestamp, selection.expires_at, selected[0]
            )
        except (ValueError, OSError) as error:
            _log.warning("Could not save promoted payment choice: %s", error)
            await error_message(interaction, "Your choice could not be saved. Please retry or contact the organizer.")
            return
        selection.choose_payment.disabled = True
        selection.choose_payment.label = "Payment method chosen"
        selection.stop()
        # The choice is saved even if Discord cannot deliver the instructions.
        try:
            await send_signup_reply(
                interaction.response.send_message,
                selection.event,
                entry,
                "Attendance confirmed",
                build_confirmation_message(selection.event, entry),
            )
        except (discord.HTTPException, discord.InteractionResponded):
            try:
                await send_signup_reply(
                    interaction.user.send,
                    selection.event,
                    entry,
                    "Attendance confirmed",
                    build_confirmation_message(selection.event, entry),
                )
            except discord.HTTPException:
                _log.warning("Payment method saved but instructions undeliverable for user %s", entry.user_id)
        if selection.message is not None:
            try:
                await selection.message.edit(view=selection)
            except discord.HTTPException:
                _log.warning("Could not close promoted payment prompt for user %s", entry.user_id)


def restore_payment_selections(client: discord.Client) -> None:
    """Register existing DM buttons after restart without resending or extending deadlines."""
    from offkai_bot.data.event import load_event_data

    now = datetime.now(UTC)
    for event in load_event_data():
        if event.archived or not _payment_methods(event):
            continue
        for entry in get_responses(event.event_name):
            if (
                entry.payment_selection_expires_at
                and not entry.payment_method
                and entry.payment_selection_expires_at > now
            ):
                client.add_view(PromotedPaymentSelection(event, entry))


class SignupContinuation(ui.View):
    """Keep validated first-page answers private until the owner finishes signup."""

    def __init__(self, signup: GatheringModal, owner_id: int):
        super().__init__(timeout=300)
        self.signup = signup
        self.owner_id = owner_id
        self.finished = False
        self.message: discord.Message | None = None
        if signup.payment_deferred:
            self.continue_signup.label = "Confirm agreement"

    @property
    def key(self) -> tuple[str, int]:
        return self.signup.event_name, self.owner_id

    def _registered(self) -> bool:
        return any(
            entry.user_id == self.owner_id
            for entry in [*get_responses(self.signup.event_name), *get_waitlist(self.signup.event_name)]
        )

    async def activate(self) -> None:
        previous = _active_signups.get(self.key)
        if self._registered():
            if previous is not None:
                await previous.finish()
            await self.finish()
            return
        _active_signups[self.key] = self
        if previous is not None and previous is not self:
            await previous.finish(superseded=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.signup.event.archived:
            await error_message(interaction, "This event is archived and no longer accepts signups.")
            return False
        if interaction.user.id == self.owner_id and not self.finished:
            return True
        await error_message(interaction, "This signup is finished or belongs to another user.")
        return False

    async def finish(self, *, expired: bool = False, superseded: bool = False) -> None:
        if self.finished:
            return
        self.finished = True
        self.continue_signup.disabled = True
        if (expired or superseded) and not self._registered():
            self.continue_signup.label = (
                "Replaced — use latest prompt" if superseded else "Expired — start signup again"
            )
        else:
            self.continue_signup.label = "Completed"
        self.stop()
        if _active_signups.get(self.key) is self:
            del _active_signups[self.key]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                _log.warning("Could not update payment prompt for user %s", self.owner_id)

    async def on_timeout(self) -> None:
        await self.finish(expired=True)

    @ui.button(label="Choose Payment Method", style=discord.ButtonStyle.primary)
    async def continue_signup(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(PaymentModal(self))


class PaymentModal(ui.Modal):
    def __init__(self, continuation: SignupContinuation):
        super().__init__(title="Payment and agreement", timeout=300)
        self.continuation = continuation
        signup = continuation.signup
        methods = {} if signup.payment_deferred else _payment_methods(signup.event)
        self.payment_method_input: ui.Select | None = None
        if methods:
            self.payment_method_input = ui.Select(
                placeholder="Choose your payment method",
                custom_id="payment_method",
                options=[discord.SelectOption(label=name) for name in methods],
                required=True,
            )
            self.add_item(ui.Label(text="Payment method", component=self.payment_method_input))
        self.no_show_input: ui.TextInput | None = None
        if signup.fields and "no_show" in signup.fields:
            self.no_show_input = ui.TextInput(
                label="I agree to the no-show / no-refund policy",
                placeholder="Type Yes: payments for no-shows are not refunded",
                custom_id="no_show_agreement",
                required=True,
            )
            self.add_item(self.no_show_input)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.continuation.interaction_check(interaction):
            return
        signup = self.continuation.signup
        if self.no_show_input is not None and self.no_show_input.value.strip().casefold() != "yes":
            await modal_error_message(
                interaction,
                signup.event_name,
                "Please agree to the no-show / no-refund policy by typing 'Yes'.",
                in_dm=signup.reply_in_dm,
            )
            return
        if self.payment_method_input is not None:
            selected = self.payment_method_input.values
            signup.payment_method = selected[0] if len(selected) == 1 else None
        signup.no_show_agreed = self.no_show_input is not None
        if await signup._submit(interaction, final=True):
            await self.continuation.finish()
