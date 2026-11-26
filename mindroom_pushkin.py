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
"""MindRoom APNs and FCM pushkin subclasses for Sygnal v0.17.0 rich notifications and call rings."""

import copy
import json
import logging
import re
import time
import weakref
from typing import TYPE_CHECKING, Any, AnyStr, Dict, List, Optional, Tuple

from aioapns import NotificationRequest
from aioapns.common import NotificationResult
from opentracing import Span

from sygnal.apnspushkin import ApnsPushkin
from sygnal.exceptions import (
    NotificationQuotaDispatchException,
    PushkinSetupException,
    TemporaryNotificationDispatchException,
)
from sygnal.gcmpushkin import (
    MAX_BYTES_PER_FIELD, MAX_FIREBASE_MESSAGE_SIZE, MAX_TRIES,
    RETRY_DELAY_BASE, RETRY_DELAY_BASE_QUOTA_EXCEEDED, APIVersion, GcmPushkin,
)
from sygnal.notifications import Device, Notification, NotificationContext
from sygnal.utils import NotificationLoggerAdapter, twisted_sleep

if TYPE_CHECKING:
    from sygnal.sygnal import Sygnal

logger = logging.getLogger(__name__)

RTC_NOTIFICATION_TYPE = "org.matrix.msc4075.rtc.notification"
ENCRYPTED_EVENT_TYPE = "m.room.encrypted"
RING_HINT_KEY = "io.mindroom.rtc.notification"
TOOL_APPROVAL_TYPE = "io.mindroom.tool_approval"
REDACTION_EVENT_TYPE = "m.room.redaction"
_INTERNAL_EVENT_PREFIXES = ("io.mindroom.", "com.mindroom.")

RING_TWEAK_SOUND = "ring"
RING_SOUND = "ring.wav"
RING_PAYLOAD_KEY = "mindroom_ring"
# Marks the alert's preview (``aps.alert.body``, or the ``m.emote`` action text in
# ``loc-args``) as already Markdown-stripped by this gateway, so the iOS
# notification service extension must not strip it a second time.
PLAIN_PAYLOAD_KEY = "mindroom_plain"

# Fields only this gateway may set on an event-generated alert. Values supplied
# through the pusher's ``default_payload`` are removed before anything else.
_RESERVED_PAYLOAD_KEYS = (
    "sender",
    "sender_display_name",
    "room_name",
    RING_PAYLOAD_KEY,
    PLAIN_PAYLOAD_KEY,
)
_RESERVED_APS_KEYS = ("thread-id", "mutable-content")
# Sygnal builds event alerts from ``loc-key``/``loc-args`` only, so a ``body``
# present before enrichment can only have come from ``default_payload``.
_RESERVED_ALERT_KEYS = (
    "body",
    "title",
    "subtitle",
    "title-loc-key",
    "title-loc-args",
    "subtitle-loc-key",
    "subtitle-loc-args",
)

MAX_RING_LIFETIME_MS = 120_000
MAX_CLOCK_SKEW_MS = 20_000
HIGH_APNS_PRIORITY = 10

MAX_STRIP_CHARS = 2000
MAX_ALERT_TITLE_BYTES = 256
MAX_ALERT_SUBTITLE_BYTES = 256
MAX_TOOL_APPROVAL_BODY_BYTES = 256
MAX_THREAD_ID_BYTES = 256
MAX_SENDER_BYTES = 256
MAX_SENDER_DISPLAY_NAME_BYTES = 128
MAX_ROOM_NAME_BYTES = 128
# Gateway metadata is only kept while Sygnal's truncation leaves at least this
# many UTF-8 bytes (16 characters or more) of every preview string.
MIN_PREVIEW_BYTES = 64

FCM_MESSAGE_CHANNEL_ID = "mindroom_messages"
FCM_CALL_CHANNEL_ID = "mindroom_calls"
FCM_ACCOUNT_KEY = "mindroom_account"
FCM_DEFAULT_TITLE = "MindRoom"
FCM_DEFAULT_BODY = "New message"
MAX_ACCOUNT_BYTES = 256
_FCM_RESERVED_DATA_KEYS = frozenset((
    *_RESERVED_PAYLOAD_KEYS,
    "room_id", "event_id", "notification", "android",
    "prio", "unread", "missed_calls", "type", "room_alias", "membership",
    "from", "message_type",
))
_FCM_FORBIDDEN_DATA_PREFIXES = ("google.", "gcm.")
_FCM_MEDIA_BODIES = {
    "m.image": "Sent an image", "m.video": "Sent a video",
    "m.audio": "Sent an audio message", "m.file": "Sent a file",
}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_LONE_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_REPLY_FALLBACK_RE = re.compile(
    r"\A> (?:\* )?<@[^\n]*\n(?:>(?: [^\n]*)?\n)*[ \t]*\n"
)
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

_DISPLAY_MATH_RE = re.compile(r"(?<!\\)\$\$([^\s$](?:[^$\n]*?[^\s$\\])?)\$\$")
_INLINE_MATH_RE = re.compile(
    r"(?<![\\$])\$([^\s$](?:[^$\n]*?[^\s$\\])?)\$(?![\d$])"
)
_IMAGE_RE = re.compile(r"!\[([^\[\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
_LINK_RE = re.compile(r"\[([^\[\]\n]*)\]\((?:[^()\n]|\([^()\n]*\))*\)")
_BOLD_ITALIC_RE = re.compile(
    r"(?<![\w*\\])\*\*\*([^\s*](?:[^*\n]*?[^\s*])?)(?<!\\)\*\*\*(?![\w*])"
)
_BOLD_RE = re.compile(
    r"(?<![\w*\\])\*\*([^\s*](?:(?:[^*\n]|\*(?!\*))*?[^\s*])?)(?<!\\)\*\*(?![\w*])"
)
_ITALIC_RE = re.compile(
    r"(?<![\w*\\])\*([^\s*](?:[^*\n]*?[^\s*])?)(?<!\\)\*(?![\w*])"
)
_STRIKETHROUGH_RE = re.compile(
    r"~~([^\s~](?:(?:[^~\n]|~(?!~))*?[^\s~])?)~~"
)
_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")

_PUA_START = 0xE000
_PUA_END = 0xF8FF


def now_ms() -> int:
    """Return the current wall-clock time in milliseconds."""
    return int(time.time() * 1000)


def _scrub_surrogates(text: str) -> str:
    """Replace lone UTF-16 surrogates, which cannot be UTF-8 encoded, with U+FFFD."""
    return _LONE_SURROGATE_RE.sub("\ufffd", text)


def _json_size(payload: Dict[str, Any]) -> int:
    """Return the size Sygnal measures for ``payload``; never raises on lone surrogates."""
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass"))


def _json_str_bytes(text: str) -> int:
    """Return UTF-8 byte length of ``text`` inside a JSON string (excluding quotes)."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _preview_floor(text: str) -> str:
    """Return the longest prefix of ``text`` within ``MIN_PREVIEW_BYTES`` UTF-8 bytes."""
    head = text[:MIN_PREVIEW_BYTES].encode("utf-8", "surrogatepass")
    return head[:MIN_PREVIEW_BYTES].decode("utf-8", "ignore")


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Truncate ``text`` cleanly on a Unicode boundary to ``max_bytes`` JSON UTF-8 bytes."""
    if max_bytes <= 0 or not text:
        return ""
    text = _scrub_surrogates(text)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes and _json_str_bytes(text) <= max_bytes:
        return text
    candidate = encoded[:max_bytes].decode("utf-8", errors="ignore")
    if _json_str_bytes(candidate) <= max_bytes:
        return candidate
    lo, hi = 0, len(candidate)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_str_bytes(candidate[:mid]) <= max_bytes:
            lo = mid
        else:
            hi = mid - 1
    return candidate[:lo]


def _clean_display_field(value: Any) -> Optional[str]:
    """Sanitize C0/DEL control characters and lone surrogates in display/room names."""
    if not isinstance(value, str) or not value:
        return None
    cleaned = _CONTROL_CHARS_RE.sub(" ", _scrub_surrogates(value)).strip()
    return cleaned or None


def _find_code_spans(line: str) -> List[Tuple[int, int, str]]:
    """Return ``(start, end, content)`` for CommonMark inline code spans in ``line``."""
    if "`" not in line:
        return []
    runs: List[Tuple[int, int]] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "`":
            j = i + 1
            while j < n and line[j] == "`":
                j += 1
            runs.append((i, j - i))
            i = j
        else:
            i += 1

    spans: List[Tuple[int, int, str]] = []
    r_idx = 0
    num_runs = len(runs)
    while r_idx < num_runs:
        start, run_len = runs[r_idx]
        close_idx = -1
        for k in range(r_idx + 1, num_runs):
            if runs[k][1] == run_len:
                close_idx = k
                break
        if close_idx == -1:
            r_idx += 1
            continue
        close_start, _ = runs[close_idx]
        content = line[start + run_len : close_start]
        if (
            len(content) >= 2
            and content[0] == " "
            and content[-1] == " "
            and content.strip(" ")
        ):
            content = content[1:-1]
        spans.append((start, close_start + run_len, content))
        r_idx = close_idx + 1
    return spans


def _split_table_cells(row_inner: str) -> List[str]:
    """Split pipe-fenced GFM table row contents without breaking ``|`` in inline code."""
    spans = _find_code_spans(row_inner)
    cells: List[str] = []
    cell_start = 0
    span_idx = 0
    num_spans = len(spans)
    i = 0
    n = len(row_inner)
    while i < n:
        while span_idx < num_spans and spans[span_idx][1] <= i:
            span_idx += 1
        if span_idx < num_spans and spans[span_idx][0] <= i < spans[span_idx][1]:
            i = spans[span_idx][1]
            continue
        if row_inner[i] == "|":
            cells.append(row_inner[cell_start:i])
            cell_start = i + 1
        i += 1
    cells.append(row_inner[cell_start:])
    return cells


def _strip_inline_markdown(line: str) -> str:
    """Strip inline Markdown from a non-code line and collapse its prose whitespace."""
    used_chars = set(line)
    next_pua = _PUA_START
    code_literals: Dict[str, str] = {}
    display_math_literals: Dict[str, str] = {}
    inline_math_literals: Dict[str, str] = {}

    def _alloc(literals: Dict[str, str], literal: str) -> Optional[str]:
        nonlocal next_pua
        while next_pua <= _PUA_END and chr(next_pua) in used_chars:
            next_pua += 1
        if next_pua > _PUA_END:
            return None
        token = chr(next_pua)
        next_pua += 1
        literals[token] = literal
        return token

    code_spans = _find_code_spans(line)
    if code_spans:
        parts: List[str] = []
        pos = 0
        for start, end, content in code_spans:
            if start > pos:
                parts.append(line[pos:start])
            token = _alloc(code_literals, content)
            parts.append(token if token is not None else content)
            pos = end
        if pos < len(line):
            parts.append(line[pos:])
        line = "".join(parts)

    if "$" in line:
        def _protect_math(literals: Dict[str, str], match: "re.Match[str]") -> str:
            token = _alloc(literals, match.group(0))
            return token if token is not None else match.group(0)

        line = _DISPLAY_MATH_RE.sub(
            lambda match: _protect_math(display_math_literals, match), line
        )
        line = _INLINE_MATH_RE.sub(
            lambda match: _protect_math(inline_math_literals, match), line
        )

    line = _IMAGE_RE.sub(r"\1", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _BOLD_ITALIC_RE.sub(r"\1", line)
    line = _BOLD_RE.sub(r"\1", line)
    line = _ITALIC_RE.sub(r"\1", line)
    line = _STRIKETHROUGH_RE.sub(r"\1", line)
    # Collapse whitespace while code and math are still tokens, keeping theirs literal.
    line = _HORIZONTAL_WS_RE.sub(" ", line)

    # A literal can hold tokens of every layer protected before it (inline math
    # wraps display math wraps code), so restore in reverse protection order.
    for literals in (inline_math_literals, display_math_literals, code_literals):
        if literals:
            line = line.translate(str.maketrans(literals))
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

        sep = 1 if kept_lines else 0
        remaining = MAX_STRIP_CHARS - kept_chars - sep
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[:remaining]
        kept_lines.append((line, in_code))
        kept_chars += sep + len(line)

    out: List[str] = []
    for line, in_code in kept_lines:
        if in_code:
            cleaned = line.rstrip(" \t")
            if cleaned.strip():
                out.append(cleaned)
            continue

        line = _BLOCKQUOTE_RE.sub("", line)
        trimmed = line.strip(" \t")
        if _TABLE_DELIM_RE.match(trimmed):
            continue
        if _TABLE_ROW_RE.match(trimmed):
            cells: List[str] = []
            for raw_cell in _split_table_cells(trimmed[1:-1]):
                cell = _strip_inline_markdown(raw_cell).strip()
                if cell:
                    cells.append(cell)
            if cells:
                out.append(" · ".join(cells))
            continue
        if _HR_RE.match(line):
            continue
        line = _ATX_HEADING_RE.sub("", line)
        line = _UNORDERED_LIST_RE.sub("• ", line)
        cleaned = _strip_inline_markdown(line).strip()
        if cleaned:
            out.append(cleaned)

    result = "\n".join(out)
    if len(result) > MAX_STRIP_CHARS:
        result = result[:MAX_STRIP_CHARS].rstrip(" \t\n")
    return _scrub_surrogates(result)


def _sygnal_generates_alert(n: Notification) -> bool:
    """Return True iff stock Sygnal v0.17.0 ApnsPushkin generates an alert for ``n``."""
    if not n.type:
        return False
    if n.type == "m.room.member":
        return bool(n.user_is_target and n.membership == "invite")
    return True


def _drop_event_reason(n: Notification) -> Optional[str]:
    """Return a log reason if ``n`` should be dropped without sending a push."""
    event_type = n.type
    if not isinstance(event_type, str):
        return None
    if event_type == TOOL_APPROVAL_TYPE:
        if not isinstance(n.content, dict) or n.content.get("status") != "pending":
            return "non-pending tool approval"
        return None
    if event_type == REDACTION_EVENT_TYPE:
        return "redaction"
    if event_type.startswith(_INTERNAL_EVENT_PREFIXES):
        return "internal"
    return None


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


def _ring_expires_at(ring: Dict[str, Any]) -> int:
    """Return the capped wall-clock expiry timestamp in ms for a validated ring."""
    return ring["sender_ts"] + min(ring["lifetime"], MAX_RING_LIFETIME_MS)


def _extract_display_fields(
    n: Notification,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return ``(sender_display_name, sender_id, room_display)``."""
    return (
        _clean_display_field(n.sender_display_name),
        n.sender if isinstance(n.sender, str) and n.sender else None,
        _clean_display_field(n.room_name) or _clean_display_field(n.room_alias),
    )


def _format_tool_approval_body(content: Dict[str, Any]) -> str:
    """Format a pending tool-approval summary line."""
    tool_name = _clean_display_field(content.get("tool_name"))
    return _truncate_utf8(
        f"Approval needed: {tool_name}" if tool_name else "Approval needed",
        MAX_TOOL_APPROVAL_BODY_BYTES,
    )


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
    def _sanitize_event_alert(
        n: Notification, payload: Dict[str, Any], aps: Dict[str, Any]
    ) -> None:
        """Drop caller-supplied gateway-owned fields and make ``loc-args`` encodable."""
        for key in _RESERVED_PAYLOAD_KEYS:
            payload.pop(key, None)
        if not n.room_id:
            # Sygnal only writes room_id when the homeserver sent one.
            payload.pop("room_id", None)
        for key in _RESERVED_APS_KEYS:
            aps.pop(key, None)
        alert = aps["alert"]
        for key in _RESERVED_ALERT_KEYS:
            alert.pop(key, None)
        loc_args = alert.get("loc-args")
        if isinstance(loc_args, list):
            alert["loc-args"] = [
                _scrub_surrogates(arg) if isinstance(arg, str) else arg
                for arg in loc_args
            ]

    def _fits_with_preview(self, payload: Dict[str, Any]) -> bool:
        """Return True iff Sygnal can fit ``payload`` without erasing its preview.

        Sygnal shortens ``alert.body`` and ``alert.loc-args``, longest first,
        until the payload fits, so none of them ends up shorter than its
        ``MIN_PREVIEW_BYTES`` prefix if the payload fits with all of them cut
        to that prefix. That takes one size measurement instead of a truncation.
        """
        aps = payload["aps"]
        alert = aps["alert"]
        body = alert.get("body", "")
        loc_args = alert.get("loc-args", [])
        choppable = [body, *loc_args] if isinstance(loc_args, list) else [None]
        # Sygnal can only shorten strings; any other alert has to fit as it is.
        if all(isinstance(text, str) for text in choppable):
            floor = dict(alert)
            if "body" in alert:
                floor["body"] = _preview_floor(body)
            if "loc-args" in alert:
                floor["loc-args"] = [_preview_floor(arg) for arg in loc_args]
            payload = {**payload, "aps": {**aps, "alert": floor}}
        return _json_size(payload) <= self.MAX_JSON_BODY_SIZE

    @staticmethod
    def _structured_alert(
        from_display: str, room_display: Optional[str], body: str
    ) -> Dict[str, Any]:
        """Build a structured title/subtitle/body APNs alert."""
        alert: Dict[str, Any] = {
            "title": _truncate_utf8(from_display, MAX_ALERT_TITLE_BYTES),
            "body": body,
        }
        if room_display and room_display != from_display:
            alert["subtitle"] = _truncate_utf8(
                room_display, MAX_ALERT_SUBTITLE_BYTES
            )
        return alert

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
        sender_display_name, sender_id, room_display = _extract_display_fields(n)
        from_display = sender_display_name or sender_id or " "

        content = n.content if isinstance(n.content, dict) else None
        if n.type == "m.room.message" and content is not None:
            msgtype = content.get("msgtype")
            body = content.get("body")
            if msgtype in ("m.text", "m.notice") and isinstance(body, str):
                clean_body = strip_markdown(body)
                if clean_body:
                    aps["alert"] = self._structured_alert(
                        from_display, room_display, clean_body
                    )
                    payload[PLAIN_PAYLOAD_KEY] = 1
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
                        payload[PLAIN_PAYLOAD_KEY] = 1
                else:
                    aps["alert"] = self._fallback_localized_alert(
                        from_display, room_display
                    )
        elif n.type == TOOL_APPROVAL_TYPE and content is not None:
            approval_body = _format_tool_approval_body(content)
            aps["alert"] = self._structured_alert(
                from_display, room_display, approval_body
            )
            payload[PLAIN_PAYLOAD_KEY] = 1

        aps["mutable-content"] = 1
        if isinstance(n.room_id, str) and n.room_id:
            aps["thread-id"] = _truncate_utf8(n.room_id, MAX_THREAD_ID_BYTES)
        if sender_id is not None:
            payload["sender"] = _truncate_utf8(sender_id, MAX_SENDER_BYTES)
        if sender_display_name is not None:
            payload["sender_display_name"] = _truncate_utf8(
                sender_display_name, MAX_SENDER_DISPLAY_NAME_BYTES
            )
        if room_display is not None:
            payload["room_name"] = _truncate_utf8(room_display, MAX_ROOM_NAME_BYTES)

    def _get_payload_full(
        self,
        n: Notification,
        device: Device,
        log: NotificationLoggerAdapter,
        send_badge_counts: bool,
    ) -> Optional[Dict[str, Any]]:
        drop_reason = _drop_event_reason(n)
        if drop_reason is not None:
            log.info("Dropping %s notification (type=%.128s)", drop_reason, n.type)
            return None
        payload = super()._get_payload_full(n, device, log, send_badge_counts)
        if payload is None:
            return None
        aps = payload.get("aps")
        if (
            not _sygnal_generates_alert(n)
            or not isinstance(aps, dict)
            or not isinstance(aps.get("alert"), dict)
        ):
            return payload

        now = now_ms()
        ring = ring_of(n, now) if device.tweaks.sound == RING_TWEAK_SOUND else None
        if ring is not None:
            expires_at = _ring_expires_at(ring)
            if expires_at <= now:
                log.info("Dropping expired ring notification (expires_at=%d)", expires_at)
                return None

            name = _scrub_surrogates(
                (n.sender_display_name or n.sender or " ")[: self.MAX_FIELD_LENGTH]
            )
            is_video = ring.get("m.call.intent") == "video"
            aps["alert"] = {
                "loc-key": "VIDEO_CALL_FROM_USER" if is_video else "VOICE_CALL_FROM_USER",
                "loc-args": [name],
            }
            aps["sound"] = RING_SOUND
            payload[RING_PAYLOAD_KEY] = {"expires_at": expires_at}
            self._dispatch_deadlines[log] = expires_at
            return payload

        # Sanitize before the snapshot so the fallback cannot restore spoofed fields.
        self._sanitize_event_alert(n, payload, aps)
        stock_payload = copy.deepcopy(payload)

        self._enrich_alert_payload(n, payload, aps)
        if self._fits_with_preview(payload):
            return payload

        payload.pop("sender_display_name", None)
        payload.pop("room_name", None)
        aps["alert"].pop("subtitle", None)
        if self._fits_with_preview(payload):
            return payload

        return stock_payload

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


def _valid_mindroom_account(value: Any) -> Optional[str]:
    if not isinstance(value, str) or _CONTROL_CHARS_RE.search(value) or _LONE_SURROGATE_RE.search(value):
        return None
    cleaned = value.strip()
    return cleaned if cleaned and _json_str_bytes(cleaned) <= MAX_ACCOUNT_BYTES else None


def _fcm_wire_size(body: Dict[str, Any]) -> int:
    return len(json.dumps(body).encode("utf-8"))


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


def _fit_fcm_body(body: Dict[str, Any], custom_data_keys: List[str]) -> None:
    """Keep the serialized FCM HTTP v1 wire body within ``MAX_FIREBASE_MESSAGE_SIZE``."""
    def _fits() -> bool:
        return _fcm_wire_size(body) <= MAX_FIREBASE_MESSAGE_SIZE

    if _fits():
        return
    msg = body["message"]
    data, notif, android = msg.get("data"), msg.get("notification"), msg.get("android")
    an = android.get("notification") if isinstance(android, dict) else None

    if isinstance(data, dict):
        for key in sorted(custom_data_keys, key=lambda k: len(data.get(k, "")), reverse=True):
            if data.pop(key, None) is not None and _fits():
                return
    if isinstance(notif, dict) and isinstance(notif.get("body"), str):
        orig = notif["body"]
        if _truncate_fcm_field(body, notif, "body") and len(notif["body"].encode("utf-8")) >= MIN_PREVIEW_BYTES:
            return
        notif["body"] = orig
    for box, key in (*((data, k) for k in ("unread", "missed_calls", "prio", PLAIN_PAYLOAD_KEY)), (an, "tag")):
        if isinstance(box, dict) and box.pop(key, None) is not None and _fits():
            return
    if isinstance(notif, dict):
        if _truncate_fcm_field(body, notif, "body"):
            return
        if isinstance(notif.get("title"), str):
            notif["title"] = _truncate_utf8(notif["title"], MIN_PREVIEW_BYTES)
            if _fits() or _truncate_fcm_field(body, notif, "body") or _truncate_fcm_field(body, notif, "title"):
                return
    if isinstance(data, dict):
        for key in (FCM_ACCOUNT_KEY, "event_id", "room_id"):
            if data.pop(key, None) is not None and _fits():
                return
        if isinstance(notif, dict):
            _truncate_fcm_field(body, notif, "body")


def _format_fcm_alert(
    n: Notification, sender_label: Optional[str], room_display: Optional[str]
) -> Tuple[str, str, bool]:
    """Return ``(title, body, is_plain)`` for a non-ring alert-bearing FCM event."""
    if room_display and sender_label and room_display != sender_label:
        half = (MAX_ALERT_TITLE_BYTES - 2) // 2
        title = f"{_truncate_utf8(room_display, half)}: {_truncate_utf8(sender_label, half)}"
    else:
        title = _truncate_utf8(sender_label or room_display or FCM_DEFAULT_TITLE, MAX_ALERT_TITLE_BYTES)

    content = n.content if isinstance(n.content, dict) else None
    if n.type == "m.room.message" and content is not None:
        msgtype, raw_body = content.get("msgtype"), content.get("body")
        if msgtype in ("m.text", "m.notice", "m.emote") and isinstance(raw_body, str):
            clean = strip_markdown(raw_body)
            if not clean:
                return title, FCM_DEFAULT_BODY, False
            if msgtype == "m.emote" and sender_label:
                clean = f"{_truncate_utf8(sender_label, MAX_SENDER_DISPLAY_NAME_BYTES)} {clean}"
            return title, clean, True
        if isinstance(msgtype, str) and msgtype in _FCM_MEDIA_BODIES:
            return title, _FCM_MEDIA_BODIES[msgtype], False
        if isinstance(raw_body, str) and (clean := _clean_display_field(raw_body)):
            return title, clean, False
    elif n.type == TOOL_APPROVAL_TYPE and content is not None:
        return title, _format_tool_approval_body(content), True
    elif n.type == "m.room.member" and n.user_is_target and n.membership == "invite":
        return title, (f"Invited you to {_truncate_utf8(room_display, MAX_ROOM_NAME_BYTES)}" if room_display else "Invited you to chat"), False
    elif n.type == "m.call.invite":
        offer = content.get("offer") if content is not None else None
        sdp = offer.get("sdp") if isinstance(offer, dict) else None
        return title, ("Incoming video call" if isinstance(sdp, str) and "m=video" in sdp else "Incoming voice call"), False
    return title, FCM_DEFAULT_BODY, False


class MindroomFcmPushkin(GcmPushkin):
    """Sygnal v0.17.0 GcmPushkin subclass for rich Android FCM HTTP v1 alerts and call rings."""

    def __init__(self, name: str, sygnal: "Sygnal", config: Dict[str, Any]) -> None:
        if config.get("api_version") != APIVersion.V1.value:
            raise PushkinSetupException("MindroomFcmPushkin requires `api_version: v1`")
        super().__init__(name, sygnal, config)

    def _build_v1_payload(
        self, n: Notification, device: Device, default_payload: Dict[str, Any], log: NotificationLoggerAdapter
    ) -> Optional[Tuple[Dict[str, Any], Optional[int]]]:
        """Build ``(wire_body, ring_expires_at_ms)`` or return None to drop."""
        room_id = n.room_id if isinstance(n.room_id, str) and n.room_id else None
        event_id = n.event_id if isinstance(n.event_id, str) and n.event_id else None
        now = now_ms()
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

        data: Dict[str, str] = {}
        custom_keys: List[str] = []
        if (account := _valid_mindroom_account(default_payload.get(FCM_ACCOUNT_KEY))) is not None:
            data[FCM_ACCOUNT_KEY] = account
        for key, value in default_payload.items():
            if (
                isinstance(key, str) and key and key != FCM_ACCOUNT_KEY
                and key not in _FCM_RESERVED_DATA_KEYS
                and not key.startswith(_FCM_FORBIDDEN_DATA_PREFIXES)
                and not _LONE_SURROGATE_RE.search(key) and isinstance(value, str)
            ):
                data[key] = _scrub_surrogates(value)
                custom_keys.append(key)

        if room_id is not None:
            data["room_id"] = _truncate_utf8(room_id, MAX_BYTES_PER_FIELD)
        if event_id is not None:
            data["event_id"] = _truncate_utf8(event_id, MAX_BYTES_PER_FIELD)
        priority = "high" if ring is not None else ("normal" if n.prio == "low" else "high")
        data.update(prio=priority, **counts)

        message: Dict[str, Any] = copy.deepcopy(self.base_request_body)
        android = dict(message["android"]) if isinstance(message.get("android"), dict) else {}
        android["priority"] = priority

        is_non_invite_member = n.type == "m.room.member" and not (n.user_is_target and n.membership == "invite")
        channel_id: Optional[str] = None
        if ring is not None or (not is_badge_only and not is_non_invite_member):
            sender_display_name, sender_id, room_display = _extract_display_fields(n)
            sender_clean = _clean_display_field(sender_id)
            if ring is not None:
                assert ring_expires_at is not None
                is_video = ring.get("m.call.intent") == "video"
                message["notification"] = {
                    "title": _truncate_utf8(sender_display_name or room_display or sender_clean or FCM_DEFAULT_TITLE, MAX_ALERT_TITLE_BYTES),
                    "body": "Incoming video call" if is_video else "Incoming voice call",
                }
                android["ttl"] = f"{(ring_expires_at - now) // 1000}s"
                channel_id = FCM_CALL_CHANNEL_ID
                data[RING_PAYLOAD_KEY] = json.dumps({"expires_at": ring_expires_at, "call_type": "video" if is_video else "voice"}, separators=(",", ":"))
            else:
                title, body_text, is_plain = _format_fcm_alert(n, sender_display_name or sender_clean, room_display)
                message["notification"] = {"title": title, "body": body_text}
                channel_id = FCM_MESSAGE_CHANNEL_ID
                if is_plain:
                    data[PLAIN_PAYLOAD_KEY] = "1"

        if channel_id is not None:
            an = dict(android["notification"]) if isinstance(android.get("notification"), dict) else {}
            an.update(channel_id=channel_id, sound="default")
            if room_id is not None:
                an["tag"] = _truncate_utf8(room_id, MAX_THREAD_ID_BYTES)
            android["notification"] = an

        message.update(android=android, data=data, token=device.pushkey)
        wire_body: Dict[str, Any] = {"message": message}
        _fit_fcm_body(wire_body, custom_keys)
        return wire_body, ring_expires_at

    async def _dispatch_notification_unlimited(
        self, n: Notification, device: Device, context: NotificationContext
    ) -> List[str]:
        if self.api_version is not APIVersion.V1:
            return await super()._dispatch_notification_unlimited(n, device, context)

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
                log.warning("Rejecting pushkey(s) due to misconfigured default_payload (expected dict).")
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
                if ring_expires_at is not None and ring_expires_at <= now_ms():
                    return _drop_expired()
                headers: Dict[AnyStr, List[AnyStr]] = {
                    "User-Agent": ["sygnal"],
                    "Content-Type": ["application/json"],
                    "Authorization": [await self._get_auth_header()],
                }
                send_body = body
                if ring_expires_at is not None:
                    now = now_ms()
                    if ring_expires_at <= now:
                        return _drop_expired()
                    ttl = f"{(ring_expires_at - now) // 1000}s"
                    send_body = {"message": {**body["message"], "android": {**body["message"]["android"], "ttl": ttl}}}

                log.info("Sending (attempt %i) => %r room:%s, event:%s", retry_number, pushkeys, n.room_id, n.event_id)
                try:
                    with self.sygnal.tracer.start_span("gcm_dispatch_try", tags={"retry_num": retry_number}, child_of=span_parent) as span:
                        new_failed, pushkeys = await self._request_dispatch(n, log, send_body, headers, pushkeys, span)
                    failed += new_failed
                    if not pushkeys:
                        break
                except (TemporaryNotificationDispatchException, NotificationQuotaDispatchException) as exc:
                    base = RETRY_DELAY_BASE_QUOTA_EXCEEDED if isinstance(exc, NotificationQuotaDispatchException) else RETRY_DELAY_BASE
                    retry_delay = max(0, exc.custom_retry_delay if exc.custom_retry_delay is not None else base * (2**retry_number))
                    if retry_number + 1 >= MAX_TRIES:
                        break
                    if ring_expires_at is not None and ring_expires_at <= now_ms() + retry_delay * 1000:
                        return _drop_expired()
                    log.warning("Temporary failure, will retry in %d seconds", retry_delay, exc_info=True)
                    span_parent.log_kv({"event": "temporary_fail", "retrying_in": retry_delay})
                    await twisted_sleep(retry_delay, twisted_reactor=self.sygnal.reactor)

            if pushkeys:
                log.info("Gave up retrying reg IDs: %r", pushkeys)
            span_parent.set_tag("gcm_num_failed", len(failed))
            return failed
