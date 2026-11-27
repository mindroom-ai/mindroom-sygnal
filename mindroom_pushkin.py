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

import copy
import json
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
