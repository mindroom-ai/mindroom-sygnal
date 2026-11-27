# -*- coding: utf-8 -*-
# Copyright 2026 MindRoom contributors
#
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""MindRoom FCM HTTP v1 pushkin subclass for Sygnal v0.17.0."""

import copy
import json
import logging
from typing import TYPE_CHECKING, Any, AnyStr, Dict, List, Optional, Tuple

from sygnal.exceptions import (
    NotificationQuotaDispatchException,
    PushkinSetupException,
    TemporaryNotificationDispatchException,
)
from sygnal.gcmpushkin import (
    MAX_BYTES_PER_FIELD,
    MAX_FIREBASE_MESSAGE_SIZE,
    MAX_TRIES,
    RETRY_DELAY_BASE,
    RETRY_DELAY_BASE_QUOTA_EXCEEDED,
    APIVersion,
    GcmPushkin,
)
from sygnal.notifications import Device, Notification, NotificationContext
from sygnal.utils import NotificationLoggerAdapter, twisted_sleep

import mindroom_pushkin
from mindroom_pushkin import (
    MAX_ALERT_TITLE_BYTES,
    MAX_ROOM_NAME_BYTES,
    MAX_SENDER_DISPLAY_NAME_BYTES,
    MAX_STRIP_CHARS,
    MAX_THREAD_ID_BYTES,
    MIN_PREVIEW_BYTES,
    PLAIN_PAYLOAD_KEY,
    RING_PAYLOAD_KEY,
    RING_TWEAK_SOUND,
    TOOL_APPROVAL_TYPE,
    _CONTROL_CHARS_RE,
    _LONE_SURROGATE_RE,
    _RESERVED_PAYLOAD_KEYS,
    _clean_display_field,
    _drop_event_reason,
    _extract_display_fields,
    _format_tool_approval_body,
    _json_str_bytes,
    _ring_expires_at,
    _scrub_surrogates,
    _truncate_utf8,
    ring_of,
    strip_markdown,
)

if TYPE_CHECKING:
    from sygnal.sygnal import Sygnal

logger = logging.getLogger(__name__)

FCM_MESSAGE_CHANNEL_ID = "mindroom_messages"
FCM_CALL_CHANNEL_ID = "mindroom_calls"
FCM_ACCOUNT_KEY = "mindroom_account"
FCM_DEFAULT_TITLE = "MindRoom"
FCM_DEFAULT_BODY = "New message"
MAX_ACCOUNT_BYTES = 256

# Gateway-owned and FCM-forbidden keys stripped from untrusted/config data maps.
_FCM_RESERVED_DATA_KEYS = frozenset((
    *_RESERVED_PAYLOAD_KEYS,
    "room_id", "event_id", "notification", "android",
    "prio", "unread", "missed_calls", "type", "room_alias", "membership",
    "from", "message_type",
))
_FCM_FORBIDDEN_DATA_PREFIXES = ("google.", "gcm.")
_FCM_MEDIA_BODIES = {
    "m.image": "Sent an image",
    "m.video": "Sent a video",
    "m.audio": "Sent an audio message",
    "m.file": "Sent a file",
}


def _valid_mindroom_account(value: Any) -> Optional[str]:
    """Return ``mindroom_account`` iff it is a clean, non-empty bounded string."""
    if (
        not isinstance(value, str)
        or _CONTROL_CHARS_RE.search(value)
        or _LONE_SURROGATE_RE.search(value)
    ):
        return None
    cleaned = value.strip()
    return cleaned if cleaned and _json_str_bytes(cleaned) <= MAX_ACCOUNT_BYTES else None


def _ring_ttl_str(expires_at: int, now: int) -> str:
    """Format remaining ring lifetime as a positive FCM Duration string."""
    return f"{max(1, (expires_at - now + 999) // 1000)}s"


def _fcm_wire_size(body: Dict[str, Any]) -> int:
    """Return the exact wire byte length sent by ``GcmPushkin._perform_http_request``."""
    return len(json.dumps(body).encode("utf-8"))


def _merge_safe_strings(
    source: Any, dest: Dict[str, str], optional_keys: List[str]
) -> None:
    """Merge non-reserved, valid UTF-8 string entries from ``source`` into ``dest``."""
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if (
            isinstance(key, str)
            and key
            and key != FCM_ACCOUNT_KEY
            and key not in _FCM_RESERVED_DATA_KEYS
            and not key.startswith(_FCM_FORBIDDEN_DATA_PREFIXES)
            and not _LONE_SURROGATE_RE.search(key)
            and isinstance(value, str)
        ):
            dest[key] = _scrub_surrogates(value)
            optional_keys.append(key)


def _truncate_fcm_field(body: Dict[str, Any], container: Dict[str, Any], key: str) -> bool:
    """Binary-search shorten ``container[key]`` in-place to fit ``MAX_FIREBASE_MESSAGE_SIZE``."""
    text = container.get(key)
    if not isinstance(text, str) or not text:
        return _fcm_wire_size(body) <= MAX_FIREBASE_MESSAGE_SIZE
    container[key] = "…"
    if _fcm_wire_size(body) > MAX_FIREBASE_MESSAGE_SIZE:
        container[key] = text
        return False
    lo, hi = 0, len(text) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        container[key] = text[:mid] + "…"
        if _fcm_wire_size(body) <= MAX_FIREBASE_MESSAGE_SIZE:
            lo = mid
        else:
            hi = mid - 1
    container[key] = text[:lo].rstrip(" \t\n") + "…"
    return True


def _fit_fcm_body(body: Dict[str, Any], optional_data_keys: List[str]) -> bool:
    """Fit ``body`` within ``MAX_FIREBASE_MESSAGE_SIZE`` in O(1) passes; return if it fits."""
    def _fits() -> bool:
        return _fcm_wire_size(body) <= MAX_FIREBASE_MESSAGE_SIZE

    if _fits():
        return True
    msg = body["message"]
    data = msg.get("data")
    notif = msg.get("notification")
    android = msg.get("android")
    an = android.get("notification") if isinstance(android, dict) else None

    if isinstance(data, dict) and optional_data_keys:
        for key in optional_data_keys:
            data.pop(key, None)
        if _fits():
            return True

    if isinstance(notif, dict) and isinstance(notif.get("body"), str):
        orig = notif["body"]
        if (
            _truncate_fcm_field(body, notif, "body")
            and len(notif["body"].encode("utf-8")) >= MIN_PREVIEW_BYTES
        ):
            return True
        notif["body"] = orig

    if isinstance(data, dict):
        for key in ("unread", "missed_calls", "prio", PLAIN_PAYLOAD_KEY):
            data.pop(key, None)
    if isinstance(an, dict):
        an.pop("tag", None)
    if _fits():
        return True

    if isinstance(notif, dict):
        if _truncate_fcm_field(body, notif, "body"):
            return True
        if isinstance(notif.get("title"), str):
            notif["title"] = _truncate_utf8(notif["title"], MIN_PREVIEW_BYTES)
            if (
                _fits()
                or _truncate_fcm_field(body, notif, "body")
                or _truncate_fcm_field(body, notif, "title")
            ):
                return True

    if isinstance(data, dict):
        for key in (FCM_ACCOUNT_KEY, "event_id", "room_id"):
            data.pop(key, None)
        if _fits():
            return True
        if isinstance(notif, dict) and _truncate_fcm_field(body, notif, "body"):
            return True

    return _fits()


def _format_fcm_alert(
    n: Notification, sender_label: Optional[str], room_display: Optional[str]
) -> Tuple[str, str, bool]:
    """Return ``(title, body, is_plain)`` for a non-ring alert-bearing FCM event."""
    if room_display and sender_label and room_display != sender_label:
        half = (MAX_ALERT_TITLE_BYTES - 2) // 2
        title = f"{_truncate_utf8(room_display, half)}: {_truncate_utf8(sender_label, half)}"
    else:
        title = _truncate_utf8(
            sender_label or room_display or FCM_DEFAULT_TITLE, MAX_ALERT_TITLE_BYTES
        )

    content = n.content if isinstance(n.content, dict) else None
    if n.type == "m.room.message" and content is not None:
        msgtype, raw_body = content.get("msgtype"), content.get("body")
        if msgtype in ("m.text", "m.notice", "m.emote") and isinstance(raw_body, str):
            clean = strip_markdown(raw_body)
            if not clean:
                return title, FCM_DEFAULT_BODY, False
            if msgtype == "m.emote" and sender_label:
                prefix = _truncate_utf8(sender_label, MAX_SENDER_DISPLAY_NAME_BYTES)
                clean = f"{prefix} {clean}"
            return title, clean, True
        if isinstance(msgtype, str) and msgtype in _FCM_MEDIA_BODIES:
            return title, _FCM_MEDIA_BODIES[msgtype], False
        if isinstance(raw_body, str) and (
            clean := _clean_display_field(raw_body[:MAX_STRIP_CHARS])
        ):
            return title, clean, False
    elif n.type == TOOL_APPROVAL_TYPE and content is not None:
        return title, _format_tool_approval_body(content), True
    elif n.type == "m.room.member" and n.user_is_target and n.membership == "invite":
        body = (
            f"Invited you to {_truncate_utf8(room_display, MAX_ROOM_NAME_BYTES)}"
            if room_display
            else "Invited you to chat"
        )
        return title, body, False
    elif n.type == "m.call.invite":
        offer = content.get("offer") if content is not None else None
        sdp = offer.get("sdp") if isinstance(offer, dict) else None
        body = "Incoming video call" if isinstance(sdp, str) and "m=video" in sdp else "Incoming voice call"
        return title, body, False
    return title, FCM_DEFAULT_BODY, False


class MindroomFcmPushkin(GcmPushkin):
    """Sygnal v0.17.0 GcmPushkin subclass for rich Android FCM HTTP v1 alerts and call rings."""

    def __init__(self, name: str, sygnal: "Sygnal", config: Dict[str, Any]) -> None:
        if config.get("api_version") != APIVersion.V1.value:
            raise PushkinSetupException("MindroomFcmPushkin requires `api_version: v1`")
        super().__init__(name, sygnal, config)

    def _build_v1_payload(
        self,
        n: Notification,
        device: Device,
        default_payload: Dict[str, Any],
        log: NotificationLoggerAdapter,
    ) -> Optional[Tuple[Dict[str, Any], Optional[int]]]:
        """Build ``(wire_body, ring_expires_at_ms)`` or return None to drop."""
        room_id = n.room_id if isinstance(n.room_id, str) and n.room_id else None
        event_id = n.event_id if isinstance(n.event_id, str) and n.event_id else None
        now = mindroom_pushkin.now_ms()
        ring = ring_of(n, now) if device.tweaks.sound == RING_TWEAK_SOUND else None
        ring_expires_at = _ring_expires_at(ring) if ring is not None else None
        if ring_expires_at is not None and ring_expires_at <= now:
            log.info("Dropping expired ring notification (expires_at=%d)", ring_expires_at)
            return None

        counts: Dict[str, str] = {}
        if self.get_config("send_badge_counts", bool, True) and getattr(n, "counts", None):
            if n.counts.unread:
                counts["unread"] = str(n.counts.unread)
            if n.counts.missed_calls:
                counts["missed_calls"] = str(n.counts.missed_calls)

        is_badge_only = ring is None and room_id is None and event_id is None
        if is_badge_only and not counts:
            return None

        message: Dict[str, Any] = copy.deepcopy(self.base_request_body)
        android = dict(message["android"]) if isinstance(message.get("android"), dict) else {}
        # Remove android.data so it cannot override gateway-built message.data under FCM v1 rules.
        raw_base_data = message.pop("data", None)
        raw_android_data = android.pop("data", None)

        data: Dict[str, str] = {}
        optional_keys: List[str] = []
        _merge_safe_strings(raw_base_data, data, optional_keys)
        _merge_safe_strings(raw_android_data, data, optional_keys)
        _merge_safe_strings(default_payload, data, optional_keys)
        if (account := _valid_mindroom_account(default_payload.get(FCM_ACCOUNT_KEY))) is not None:
            data[FCM_ACCOUNT_KEY] = account
        if room_id is not None:
            data["room_id"] = _truncate_utf8(room_id, MAX_BYTES_PER_FIELD)
        if event_id is not None:
            data["event_id"] = _truncate_utf8(event_id, MAX_BYTES_PER_FIELD)
        priority = "high" if ring is not None else ("normal" if n.prio == "low" else "high")
        data.update(prio=priority, **counts)
        android["priority"] = priority

        is_non_invite_member = n.type == "m.room.member" and not (
            n.user_is_target and n.membership == "invite"
        )
        channel_id: Optional[str] = None
        if ring is not None or (not is_badge_only and not is_non_invite_member):
            sender_display_name, sender_id, room_display = _extract_display_fields(n)
            sender_clean = _clean_display_field(sender_id)
            if ring is not None:
                assert ring_expires_at is not None
                is_video = ring.get("m.call.intent") == "video"
                ring_title = _truncate_utf8(
                    sender_display_name or room_display or sender_clean or FCM_DEFAULT_TITLE,
                    MAX_ALERT_TITLE_BYTES,
                )
                message["notification"] = {
                    "title": ring_title,
                    "body": "Incoming video call" if is_video else "Incoming voice call",
                }
                android["ttl"] = _ring_ttl_str(ring_expires_at, now)
                channel_id = FCM_CALL_CHANNEL_ID
                data[RING_PAYLOAD_KEY] = json.dumps(
                    {"expires_at": ring_expires_at, "call_type": "video" if is_video else "voice"},
                    separators=(",", ":"),
                )
            else:
                title, body_text, is_plain = _format_fcm_alert(
                    n, sender_display_name or sender_clean, room_display
                )
                message["notification"] = {"title": title, "body": body_text}
                channel_id = FCM_MESSAGE_CHANNEL_ID
                if is_plain:
                    data[PLAIN_PAYLOAD_KEY] = "1"

        if channel_id is not None:
            an = (
                dict(android["notification"])
                if isinstance(android.get("notification"), dict)
                else {}
            )
            an.update(channel_id=channel_id, sound="default")
            if room_id is not None:
                an["tag"] = _truncate_utf8(room_id, MAX_THREAD_ID_BYTES)
            else:
                an.pop("tag", None)
            android["notification"] = an
        else:
            # Keep data-only / badge-only pushes strictly banner-free even if fcm_options set them.
            message.pop("notification", None)
            android.pop("notification", None)

        message.update(android=android, data=data, token=device.pushkey)
        wire_body: Dict[str, Any] = {"message": message}
        if not _fit_fcm_body(wire_body, optional_keys):
            log.warning(
                "Dropping oversized FCM v1 payload (%d > %d bytes)",
                _fcm_wire_size(wire_body),
                MAX_FIREBASE_MESSAGE_SIZE,
            )
            return None
        return wire_body, ring_expires_at

    async def _dispatch_notification_unlimited(
        self, n: Notification, device: Device, context: NotificationContext
    ) -> List[str]:
        log = NotificationLoggerAdapter(logger, {"request_id": context.request_id})
        pushkeys = [device.pushkey]
        with self.sygnal.tracer.start_span(
            "gcm_dispatch", tags={"gcm_num_devices": 1}, child_of=context.opentracing_span
        ) as span_parent:
            if (drop_reason := _drop_event_reason(n)) is not None:
                log.info("Dropping %s notification (type=%.128s)", drop_reason, n.type)
                return []

            default_payload = device.data.get("default_payload", {}) if device.data else {}
            if not isinstance(default_payload, dict):
                log.warning(
                    "Rejecting pushkey(s) due to misconfigured default_payload (expected dict)."
                )
                return pushkeys

            built = self._build_v1_payload(n, device, default_payload, log)
            if built is None:
                return []
            body, ring_expires_at = built

            def _drop_expired() -> List[str]:
                assert ring_expires_at is not None
                log.info("Dropping expired ring notification (expires_at=%d)", ring_expires_at)
                span_parent.log_kv({"event": "mindroom_ring_expired"})
                span_parent.set_tag("gcm_num_failed", 0)
                return []

            failed: List[str] = []
            for retry_number in range(MAX_TRIES):
                if ring_expires_at is not None and ring_expires_at <= mindroom_pushkin.now_ms():
                    return _drop_expired()
                headers: Dict[AnyStr, List[AnyStr]] = {
                    "User-Agent": ["sygnal"],
                    "Content-Type": ["application/json"],
                    "Authorization": [await self._get_auth_header()],
                }
                send_body = body
                if ring_expires_at is not None:
                    now = mindroom_pushkin.now_ms()
                    if ring_expires_at <= now:
                        return _drop_expired()
                    send_body = {
                        "message": {
                            **body["message"],
                            "android": {
                                **body["message"]["android"],
                                "ttl": _ring_ttl_str(ring_expires_at, now),
                            },
                        }
                    }

                log.info(
                    "Sending (attempt %i) => %r room:%s, event:%s",
                    retry_number, pushkeys, n.room_id, n.event_id,
                )
                try:
                    with self.sygnal.tracer.start_span(
                        "gcm_dispatch_try",
                        tags={"retry_num": retry_number},
                        child_of=span_parent,
                    ) as span:
                        new_failed, pushkeys = await self._request_dispatch(
                            n, log, send_body, headers, pushkeys, span
                        )
                    failed += new_failed
                    if not pushkeys:
                        break
                except (
                    TemporaryNotificationDispatchException,
                    NotificationQuotaDispatchException,
                ) as exc:
                    base = (
                        RETRY_DELAY_BASE_QUOTA_EXCEEDED
                        if isinstance(exc, NotificationQuotaDispatchException)
                        else RETRY_DELAY_BASE
                    )
                    retry_delay = max(
                        0,
                        exc.custom_retry_delay
                        if exc.custom_retry_delay is not None
                        else base * (2**retry_number),
                    )
                    if retry_number + 1 >= MAX_TRIES:
                        break
                    if (
                        ring_expires_at is not None
                        and ring_expires_at <= mindroom_pushkin.now_ms() + retry_delay * 1000
                    ):
                        return _drop_expired()
                    log.warning(
                        "Temporary failure, will retry in %d seconds", retry_delay, exc_info=True
                    )
                    span_parent.log_kv({"event": "temporary_fail", "retrying_in": retry_delay})
                    await twisted_sleep(retry_delay, twisted_reactor=self.sygnal.reactor)

            if pushkeys:
                log.info("Gave up retrying reg IDs: %r", pushkeys)
            span_parent.set_tag("gcm_num_failed", len(failed))
            return failed
