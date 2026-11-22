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
"""MindRoom APNs pushkin subclass for Sygnal v0.17.0 rich notifications and call rings."""

import re
import time
import weakref
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from aioapns import NotificationRequest
from aioapns.common import NotificationResult
from opentracing import Span

from sygnal.apnspushkin import ApnsPushkin
from sygnal.notifications import Device, Notification
from sygnal.utils import NotificationLoggerAdapter

if TYPE_CHECKING:
    from sygnal.sygnal import Sygnal

RTC_NOTIFICATION_TYPE = "org.matrix.msc4075.rtc.notification"
ENCRYPTED_EVENT_TYPE = "m.room.encrypted"
RING_HINT_KEY = "io.mindroom.rtc.notification"

RING_TWEAK_SOUND = "ring"
RING_SOUND = "ring.wav"
RING_PAYLOAD_KEY = "mindroom_ring"

MAX_RING_LIFETIME_MS = 120_000
MAX_CLOCK_SKEW_MS = 20_000
HIGH_APNS_PRIORITY = 10

MAX_STRIP_CHARS = 2000
MAX_ALERT_TITLE_BYTES = 256
MAX_ALERT_SUBTITLE_BYTES = 256
MAX_THREAD_ID_BYTES = 256
MAX_SENDER_BYTES = 256
MAX_SENDER_DISPLAY_NAME_BYTES = 128
MAX_ROOM_NAME_BYTES = 128

_REPLY_FALLBACK_RE = re.compile(r"\A> <@[^\n]*\n(?:>(?: [^\n]*)?\n)*[ \t]*\n")
_TOOL_CALL_LINE_RE = re.compile(
    r"^🔧[^\S\n]*`[^`\n]+`[^\S\n]*\[\d+\](?:[^\S\n]*⏳)?[^\S\n]*$"
)
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?:(`{3,})[^`\n]*|(~{3,})[^\n]*)$")
_TABLE_DELIM_RE = re.compile(r"^\|(?:[^\S\n]*:?-+:?[^\S\n]*\|)+$")
_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_HR_RE = re.compile(r"^[^\S\n]*(?:[-*_][^\S\n]*){3,}$")
_ATX_HEADING_RE = re.compile(r"^[^\S\n]{0,3}#{1,6}[^\S\n]+")
_BLOCKQUOTE_RE = re.compile(r"^[^\S\n]*(?:>[^\S\n]?)+")
_UNORDERED_LIST_RE = re.compile(r"^[^\S\n]*[-*+][^\S\n]+")

_IMAGE_RE = re.compile(r"!\[([^\[\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_ITALIC_RE = re.compile(
    r"(?<![\w*])\*\*\*([^\s*](?:[^*\n]*?[^\s*])?)\*\*\*(?![\w*])"
)
_BOLD_RE = re.compile(
    r"(?<![\w*])\*\*([^\s*](?:(?:[^*\n]|\*(?!\*))*?[^\s*])?)\*\*(?![\w*])"
)
_ITALIC_RE = re.compile(
    r"(?<![\w*])\*([^\s*](?:[^*\n]*?[^\s*])?)\*(?![\w*])"
)
_STRIKETHROUGH_RE = re.compile(
    r"~~([^\s~](?:(?:[^~\n]|~(?!~))*?[^\s~])?)~~"
)
_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")
_NULL_RUN_RE = re.compile(r"\x00+")


def now_ms() -> int:
    """Return the current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` cleanly on a Unicode character boundary to ``max_bytes``."""
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _strip_inline_markdown(line: str) -> str:
    """Strip inline Markdown syntax from a single non-code line."""
    code_spans: List[str] = []
    sentinel = "\x00"
    if "`" in line:
        if sentinel in line:
            max_run = max(len(m.group(0)) for m in _NULL_RUN_RE.finditer(line))
            sentinel = "\x00" * (max_run + 1)

        def _save_code(match: "re.Match[str]") -> str:
            idx = len(code_spans)
            code_spans.append(match.group(1))
            return f"{sentinel}C{idx}{sentinel}"

        line = _INLINE_CODE_RE.sub(_save_code, line)

    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _BOLD_ITALIC_RE.sub(r"\1", line)
    line = _BOLD_RE.sub(r"\1", line)
    line = _ITALIC_RE.sub(r"\1", line)
    line = _STRIKETHROUGH_RE.sub(r"\1", line)

    if code_spans:
        escaped = re.escape(sentinel)
        line = re.sub(
            f"{escaped}C(\\d+){escaped}",
            lambda m: code_spans[int(m.group(1))],
            line,
        )
    return line


def strip_markdown(text: str) -> str:
    """Strip Markdown and Matrix/MindRoom boilerplate for clean notification display."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _REPLY_FALLBACK_RE.sub("", text, count=1)

    kept_lines: List[Tuple[str, bool]] = []
    kept_chars = 0
    fence_char = ""
    fence_len = 0

    for line in text.split("\n"):
        if fence_char:
            stripped_left = line.lstrip(" ")
            indent = len(line) - len(stripped_left)
            if indent <= 3:
                candidate = stripped_left.rstrip(" \t")
                if len(candidate) >= fence_len and set(candidate) == {fence_char}:
                    fence_char = ""
                    fence_len = 0
                    continue
            in_code = True
        else:
            fence_match = _FENCE_OPEN_RE.match(line)
            if fence_match:
                marker = fence_match.group(1) or fence_match.group(2)
                fence_char = marker[0]
                fence_len = len(marker)
                continue
            if _TOOL_CALL_LINE_RE.match(line):
                continue
            in_code = False

        if not line.strip():
            continue

        remaining = MAX_STRIP_CHARS - kept_chars
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining]
        kept_lines.append((line, in_code))
        kept_chars += len(line)

    out: List[str] = []
    for line, in_code in kept_lines:
        if in_code:
            cleaned = _HORIZONTAL_WS_RE.sub(" ", line).strip()
            if cleaned:
                out.append(cleaned)
            continue

        line = _BLOCKQUOTE_RE.sub("", line)
        trimmed = line.strip(" \t")
        if _TABLE_DELIM_RE.match(trimmed):
            continue
        if _TABLE_ROW_RE.match(trimmed):
            cells: List[str] = []
            for raw_cell in trimmed[1:-1].split("|"):
                cell = _HORIZONTAL_WS_RE.sub(" ", _strip_inline_markdown(raw_cell)).strip()
                if cell:
                    cells.append(cell)
            if cells:
                out.append(" · ".join(cells))
            continue
        if _HR_RE.match(line):
            continue
        line = _ATX_HEADING_RE.sub("", line)
        line = _UNORDERED_LIST_RE.sub("• ", line)
        line = _strip_inline_markdown(line)
        cleaned = _HORIZONTAL_WS_RE.sub(" ", line).strip()
        if cleaned:
            out.append(cleaned)

    return "\n".join(out).strip()


def ring_of(n: Notification, now: int) -> Optional[Dict[str, Any]]:
    """Return validated ring fields for ``n``, or None if it is not a valid ring."""
    content = n.content
    if not isinstance(content, dict):
        return None
    if n.type == RTC_NOTIFICATION_TYPE:
        ring = content
    elif n.type == ENCRYPTED_EVENT_TYPE:
        ring = content.get(RING_HINT_KEY)
    else:
        return None
    if not isinstance(ring, dict) or ring.get("notification_type") != "ring":
        return None
    sender_ts = ring.get("sender_ts")
    lifetime = ring.get("lifetime")
    if (
        type(sender_ts) is not int
        or type(lifetime) is not int
        or sender_ts <= 0
        or lifetime <= 0
        or sender_ts > now + MAX_CLOCK_SKEW_MS
    ):
        return None
    return ring


class MindroomApnsPushkin(ApnsPushkin):
    """Sygnal v0.17.0 ApnsPushkin subclass adding rich alerts and call-ring alerts.

    Deadline invariant: a ring is never sent by a Sygnal attempt that starts
    after its deadline (checked at payload build time and before every APNs
    attempt, including retries), and ``apns-expiration`` comes from
    ``request.time_to_live`` set in ``_send_notification``; up to a few seconds
    of slack inside aioapns's own reconnect/JWT path is accepted.
    """

    def __init__(self, name: str, sygnal: "Sygnal", config: Dict[str, Any]) -> None:
        super().__init__(name, sygnal, config)
        # Validated ring deadlines tracked out-of-band so caller-supplied
        # default_payload fields can never affect gateway dispatch behavior.
        self._dispatch_deadlines: "weakref.WeakKeyDictionary[NotificationLoggerAdapter, int]" = (
            weakref.WeakKeyDictionary()
        )
        self._request_deadlines: Dict[str, int] = {}

    @staticmethod
    def _fallback_localized_alert(
        from_display: str, room_display: Optional[str]
    ) -> Dict[str, Any]:
        """Build a stock localized fallback alert when cleaned body is empty."""
        sender_arg = _truncate_utf8(from_display, MAX_ALERT_TITLE_BYTES)
        if room_display:
            return {
                "loc-key": "MSG_FROM_USER_IN_ROOM",
                "loc-args": [
                    sender_arg,
                    _truncate_utf8(room_display, MAX_ALERT_SUBTITLE_BYTES),
                ],
            }
        return {
            "loc-key": "MSG_FROM_USER",
            "loc-args": [sender_arg],
        }

    def _enrich_alert_payload(
        self,
        n: Notification,
        payload: Dict[str, Any],
        aps: Dict[str, Any],
    ) -> None:
        """Enrich a non-ring alert-bearing APNs payload for rich iOS rendering."""
        sender_display_name = (
            n.sender_display_name
            if isinstance(n.sender_display_name, str) and n.sender_display_name
            else None
        )
        sender_id = n.sender if isinstance(n.sender, str) and n.sender else None
        from_display = sender_display_name or sender_id or " "

        room_display: Optional[str] = None
        if isinstance(n.room_name, str) and n.room_name:
            room_display = n.room_name
        elif isinstance(n.room_alias, str) and n.room_alias:
            room_display = n.room_alias

        content = n.content if isinstance(n.content, dict) else None
        if n.type == "m.room.message" and content is not None:
            msgtype = content.get("msgtype")
            body = content.get("body")
            if msgtype in ("m.text", "m.notice") and isinstance(body, str):
                clean_body = strip_markdown(body)
                if clean_body:
                    alert: Dict[str, Any] = {
                        "title": _truncate_utf8(from_display, MAX_ALERT_TITLE_BYTES),
                        "body": clean_body,
                    }
                    if room_display and room_display != from_display:
                        alert["subtitle"] = _truncate_utf8(
                            room_display, MAX_ALERT_SUBTITLE_BYTES
                        )
                    aps["alert"] = alert
                else:
                    aps["alert"] = self._fallback_localized_alert(
                        from_display, room_display
                    )
            elif msgtype == "m.emote" and isinstance(body, str):
                clean_body = strip_markdown(body)
                if clean_body:
                    alert_obj = aps.get("alert")
                    if (
                        isinstance(alert_obj, dict)
                        and alert_obj.get("loc-key")
                        in ("ACTION_FROM_USER", "ACTION_FROM_USER_IN_ROOM")
                        and isinstance(alert_obj.get("loc-args"), list)
                        and alert_obj["loc-args"]
                    ):
                        alert_obj["loc-args"][-1] = clean_body
                else:
                    aps["alert"] = self._fallback_localized_alert(
                        from_display, room_display
                    )

        aps["mutable-content"] = 1
        if isinstance(n.room_id, str) and n.room_id:
            aps["thread-id"] = _truncate_utf8(n.room_id, MAX_THREAD_ID_BYTES)
        else:
            aps.pop("thread-id", None)

        if sender_id is not None:
            payload["sender"] = _truncate_utf8(sender_id, MAX_SENDER_BYTES)
        else:
            payload.pop("sender", None)

        if sender_display_name is not None:
            payload["sender_display_name"] = _truncate_utf8(
                sender_display_name, MAX_SENDER_DISPLAY_NAME_BYTES
            )
        else:
            payload.pop("sender_display_name", None)

        if room_display is not None:
            payload["room_name"] = _truncate_utf8(room_display, MAX_ROOM_NAME_BYTES)
        else:
            payload.pop("room_name", None)

    def _get_payload_full(
        self,
        n: Notification,
        device: Device,
        log: NotificationLoggerAdapter,
        send_badge_counts: bool,
    ) -> Optional[Dict[str, Any]]:
        payload = super()._get_payload_full(n, device, log, send_badge_counts)
        if payload is None:
            return None
        aps = payload.get("aps")
        if not isinstance(aps, dict) or "alert" not in aps:
            return payload

        now = now_ms()
        ring = ring_of(n, now) if device.tweaks.sound == RING_TWEAK_SOUND else None
        if ring is not None:
            expires_at = ring["sender_ts"] + min(ring["lifetime"], MAX_RING_LIFETIME_MS)
            if expires_at <= now:
                log.info("Dropping expired ring notification (expires_at=%d)", expires_at)
                return None

            name = (n.sender_display_name or n.sender or " ")[: self.MAX_FIELD_LENGTH]
            is_video = ring.get("m.call.intent") == "video"
            aps["alert"] = {
                "loc-key": "VIDEO_CALL_FROM_USER" if is_video else "VOICE_CALL_FROM_USER",
                "loc-args": [name],
            }
            aps["sound"] = RING_SOUND
            payload[RING_PAYLOAD_KEY] = {"expires_at": expires_at}
            self._dispatch_deadlines[log] = expires_at
            return payload

        self._enrich_alert_payload(n, payload, aps)
        return payload

    async def _dispatch_request(
        self,
        log: NotificationLoggerAdapter,
        span: Span,
        device: Device,
        shaved_payload: Dict[str, Any],
        prio: int,
        notif_id: str,
    ) -> List[str]:
        expires_at = self._dispatch_deadlines.get(log)
        if expires_at is None:
            return await super()._dispatch_request(
                log, span, device, shaved_payload, prio, notif_id
            )

        if expires_at <= now_ms():
            log.info("Dropping expired ring notification (expires_at=%d)", expires_at)
            span.log_kv({"event": "mindroom_ring_expired"})
            return []

        self._request_deadlines[notif_id] = expires_at
        try:
            return await super()._dispatch_request(
                log, span, device, shaved_payload, HIGH_APNS_PRIORITY, notif_id
            )
        finally:
            self._request_deadlines.pop(notif_id, None)

    async def _send_notification(
        self, request: NotificationRequest
    ) -> NotificationResult:
        expires_at = self._request_deadlines.get(request.notification_id)
        if expires_at is not None:
            request.time_to_live = max(0, (expires_at // 1000) - (now_ms() // 1000))
        return await super()._send_notification(request)
