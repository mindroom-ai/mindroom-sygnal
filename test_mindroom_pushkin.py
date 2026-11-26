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
"""Table-driven unit tests for MindroomApnsPushkin inside Sygnal v0.17.0."""

import asyncio
import copy
import importlib
import json
import logging
import random
import time
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from aioapns import NotificationRequest
from aioapns.common import NotificationResult
from twisted.internet.defer import ensureDeferred
from twisted.internet.testing import MemoryReactorClock

import mindroom_pushkin
from mindroom_pushkin import (
    ENCRYPTED_EVENT_TYPE,
    MAX_ALERT_SUBTITLE_BYTES,
    MAX_ALERT_TITLE_BYTES,
    MAX_CLOCK_SKEW_MS,
    MAX_RING_LIFETIME_MS,
    MAX_ROOM_NAME_BYTES,
    MAX_SENDER_BYTES,
    MAX_SENDER_DISPLAY_NAME_BYTES,
    MAX_THREAD_ID_BYTES,
    MAX_TOOL_APPROVAL_BODY_BYTES,
    MIN_PREVIEW_BYTES,
    PLAIN_PAYLOAD_KEY,
    REDACTION_EVENT_TYPE,
    RING_HINT_KEY,
    RING_PAYLOAD_KEY,
    RING_SOUND,
    RTC_NOTIFICATION_TYPE,
    TOOL_APPROVAL_TYPE,
    FCM_ACCOUNT_KEY,
    FCM_CALL_CHANNEL_ID,
    FCM_MESSAGE_CHANNEL_ID,
    MAX_ACCOUNT_BYTES,
    MindroomApnsPushkin,
    MindroomFcmPushkin,
    _clean_display_field,
    _truncate_utf8,
    strip_markdown,
)
from twisted.python.failure import Failure
from twisted.web.client import ResponseDone
from twisted.web.http_headers import Headers

from sygnal import apnstruncate
from sygnal.apnspushkin import ApnsPushkin
from sygnal.apnstruncate import BodyTooLongException
from sygnal.exceptions import PushkinSetupException
from sygnal.gcmpushkin import MAX_FIREBASE_MESSAGE_SIZE, GcmPushkin
from sygnal.notifications import Notification, NotificationContext
from sygnal.sygnal import CONFIG_DEFAULTS, Sygnal, merge_left_with_defaults
from sygnal.utils import NotificationLoggerAdapter

APP_ID = "chat.mindroom.app"
STOCK_APP_ID = "chat.mindroom.app.stock"
FAKE_KEYFILE = "/sygnal/test-apns-key.p8"
BASE_NOW_MS = 1_795_300_000_000


def _dev(
    app_id: str = APP_ID,
    tweaks: Optional[Dict[str, Any]] = None,
    default_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    d: Dict[str, Any] = {"app_id": app_id, "pushkey": "deadbeef0123", "pushkey_ts": 42}
    if tweaks is not None:
        d["tweaks"] = tweaks
    if default_payload is not None:
        d["data"] = {"default_payload": default_payload}
    return d


def _raw(
    *,
    event_type: Optional[str] = RTC_NOTIFICATION_TYPE,
    content: Optional[Dict[str, Any]] = None,
    sender: Optional[str] = "@alice:example.com",
    sender_display_name: Optional[str] = "Alice",
    room_name: Optional[str] = None,
    room_alias: Optional[str] = None,
    room_id: Optional[str] = "!room:example.com",
    user_is_target: Optional[bool] = None,
    membership: Optional[str] = None,
    prio: str = "high",
    tweaks: Optional[Dict[str, Any]] = None,
    default_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw: Dict[str, Any] = {
        "event_id": "$event1",
        "prio": prio,
        "counts": {"unread": 1},
        "devices": [_dev(tweaks=tweaks, default_payload=default_payload)],
    }
    if room_id is not None:
        raw["room_id"] = room_id
    if event_type is not None:
        raw["type"] = event_type
    if content is not None:
        raw["content"] = content
    if sender is not None:
        raw["sender"] = sender
    if sender_display_name is not None:
        raw["sender_display_name"] = sender_display_name
    if room_name is not None:
        raw["room_name"] = room_name
    if room_alias is not None:
        raw["room_alias"] = room_alias
    if user_is_target is not None:
        raw["user_is_target"] = user_is_target
    if membership is not None:
        raw["membership"] = membership
    return raw


def _ring(sender_ts: int, lifetime: int = 30_000, intent: Optional[str] = "audio") -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "notification_type": "ring",
        "sender_ts": sender_ts,
        "lifetime": lifetime,
    }
    if intent is not None:
        fields["m.call.intent"] = intent
    return fields


def _enc(hint: Any) -> Dict[str, Any]:
    return {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque", RING_HINT_KEY: hint}


# Every gateway-owned field a pusher could try to pre-seed through default_payload.
# All spoofed values contain "spoof" so tests can assert none of them survive.
SPOOFED_DEFAULT_PAYLOAD: Dict[str, Any] = {
    "sender": "@spoof:evil.example",
    "sender_display_name": "Spoofed Admin",
    "room_name": "Spoofed Room",
    RING_PAYLOAD_KEY: {"expires_at": 1, "spoof": True},
    PLAIN_PAYLOAD_KEY: "spoof",
    "room_id": "!spoof:evil.example",
    "custom": "kept",
    "aps": {
        "mutable-content": 0,
        "thread-id": "!spoof:evil.example",
        "sound": "default",
        "alert": {
            "body": "Spoofed body",
            "title": "Spoofed Title",
            "subtitle": "Spoofed Subtitle",
            "title-loc-key": "SPOOF_TITLE",
            "title-loc-args": ["spoof"],
            "subtitle-loc-key": "SPOOF_SUBTITLE",
            "subtitle-loc-args": ["spoof"],
            "action-loc-key": "OPEN",
        },
    },
}
REAL_IDENTITY: Dict[str, Any] = {
    "sender": "@real:example.com",
    "sender_display_name": "Real User",
    "room_name": "Real Room",
    "room_id": "!real:example.com",
}


def _json_size(message: Dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False).encode("utf-8"))


def _without_reserved_fields(
    stock_payload: Dict[str, Any], *, homeserver_sent_room_id: bool = True
) -> Dict[str, Any]:
    """Return a stock payload minus every gateway-owned field."""
    payload = copy.deepcopy(stock_payload)
    for key in ("sender", "sender_display_name", "room_name", RING_PAYLOAD_KEY, PLAIN_PAYLOAD_KEY):
        payload.pop(key, None)
    if not homeserver_sent_room_id:
        payload.pop("room_id", None)
    for key in ("thread-id", "mutable-content"):
        payload["aps"].pop(key, None)
    for key in (
        "body",
        "title",
        "subtitle",
        "title-loc-key",
        "title-loc-args",
        "subtitle-loc-key",
        "subtitle-loc-args",
    ):
        payload["aps"]["alert"].pop(key, None)
    return payload


class StripMarkdownTestCase(unittest.TestCase):
    def test_markdown_stripping_cases(self) -> None:
        cases = [
            ("plain_text", "Hello world", "Hello world"),
            ("empty", "", ""),
            ("whitespace_only", "   \n\t\n   ", ""),
            ("bold", "Hello **world**!", "Hello world!"),
            ("italic", "Hello *world*!", "Hello world!"),
            ("bold_italic", "***very important*** news", "very important news"),
            ("nested_bold_italic", "**bold with *italic* inside**", "bold with italic inside"),
            ("nested_italic_bold", "*italic with **bold** inside*", "italic with bold inside"),
            ("single_char_emphasis", "*a* **b** ***c***", "a b c"),
            (
                "underscores_preserved",
                "Call __init__ and snake_case_var or _private_",
                "Call __init__ and snake_case_var or _private_",
            ),
            ("bold_around_dunder", "**__init__**", "__init__"),
            (
                "math_preserved",
                "x**2 + y**2 and 2*3*4 and 2 * 3 * 4",
                "x**2 + y**2 and 2*3*4 and 2 * 3 * 4",
            ),
            (
                "headings",
                "# H1\n### H3\n###### H6\n#hashtag\n####### seven",
                "H1\nH3\nH6\n#hashtag\n####### seven",
            ),
            ("links", "See [MindRoom](https://mindroom.chat) now", "See MindRoom now"),
            (
                "balanced_paren_links",
                "Read [Python](https://en.wikipedia.org/wiki/Python_(programming_language)) docs",
                "Read Python docs",
            ),
            (
                "images",
                "Look ![diagram](https://example.com/a(1).png) and ![](https://example.com/empty.png)",
                "Look diagram and",
            ),
            (
                "inline_code_preserves_inner_syntax",
                "Run `git status`, `**kwargs`, `a * b`, and `[x](y)`",
                "Run git status, **kwargs, a * b, and [x](y)",
            ),
            ("bold_around_inline_code", "**`critical`** and [`fn`](https://example.com)", "critical and fn"),
            (
                "backtick_code_fence",
                "Before\n```python\n# comment\nx = **kwargs\n🔧 `shell` [1]\n```\n**After**",
                "Before\n# comment\nx = **kwargs\n🔧 `shell` [1]\nAfter",
            ),
            (
                "tilde_code_fence",
                "~~~md\n**literal**\n- item\n~~~\n**stripped**",
                "**literal**\n- item\nstripped",
            ),
            (
                "tables",
                "| **Name** | Status |  |\n| --- | :---: | ---: |\n| `app.py` | ~~old~~ **new** |  |",
                "Name · Status\napp.py · old new",
            ),
            (
                "table_cell_isolation",
                "| a * b | c * d |\n| --- | --- |\n| x | y |",
                "a * b · c * d\nx · y",
            ),
            (
                "blockquotes",
                "> Quoted line\n> > Nested\n\nFollow-up",
                "Quoted line\nNested\nFollow-up",
            ),
            (
                "unordered_lists",
                "- first\n  * second\n+ third",
                "• first\n• second\n• third",
            ),
            ("strikethrough", "~~removed~~ and ~~a~b~~ kept", "removed and a~b kept"),
            (
                "horizontal_rules",
                "Above\n---\n***\n___\n- - -\nBelow",
                "Above\nBelow",
            ),
            ("horizontal_rule_only", "---", ""),
            (
                "matrix_reply_fallback",
                "> <@alice:example.com> original line 1\n> original line 2\n\n**My reply**",
                "My reply",
            ),
            (
                "matrix_reply_fallback_with_empty_quoted_line",
                "> <@alice:example.com> line 1\n>\n> line 2\n\nReply",
                "Reply",
            ),
            (
                "matrix_reply_without_separator_preserved",
                "> <@alice:example.com> lone quote",
                "<@alice:example.com> lone quote",
            ),
            (
                "normal_blockquote_preserved",
                "> Someone said\n\nAnd comment",
                "Someone said\nAnd comment",
            ),
            (
                "tool_call_markers",
                "🔧 `shell` [1]\n🔧 `read_file` [42] ⏳\nActual **answer**",
                "Actual answer",
            ),
            (
                "tool_call_markers_only",
                "🔧 `shell` [1]\n🔧 `search` [2] ⏳",
                "",
            ),
            (
                "blank_lines_and_horizontal_ws",
                "  First   line\t\there  \n\n\n   Second\tline   \n\n",
                "First line here\nSecond line",
            ),
            ("crlf_line_endings", "# Title\r\n\r\n**Body**\r\n", "Title\nBody"),
            (
                "matrix_emote_reply_fallback",
                "> * <@alice:example.com> waves hello\n> continued\n\n**My reply**",
                "My reply",
            ),
            (
                "code_fence_preserves_indentation_and_spaces",
                "```python\n    def foo():\n        return  1\n```",
                "    def foo():\n        return  1",
            ),
            (
                "table_pipe_inside_inline_code",
                "| `a|b` | **c** |\n| --- | --- |\n| ``a|b ` c`` | d |",
                "a|b · c\na|b ` c · d",
            ),
            (
                "double_backtick_inline_code",
                "Use ``a ` b`` and `` `foo` `` here",
                "Use a ` b and `foo` here",
            ),
            (
                "nul_and_empty_image_with_inline_code_no_collision",
                "\x00![](x)\x00`code` and \x00C999\x00![](x)\x00`ok`",
                "\x00\x00code and \x00C999\x00\x00ok",
            ),
            (
                "escaped_asterisks_preserved",
                r"a\*b\* and \*not italic\*",
                r"a\*b\* and \*not italic\*",
            ),
            (
                "inline_and_display_math_preserved",
                "Inline $a *b* c$ and display $$x *y* z$$",
                "Inline $a *b* c$ and display $$x *y* z$$",
            ),
            (
                "currency_not_treated_as_math",
                "Costs **$5** and **$10**; between $5 and *almost* $10",
                "Costs $5 and $10; between $5 and almost $10",
            ),
            (
                "indented_tool_call_marker_stays_prose",
                "  🔧 `shell` [1]",
                "🔧 shell [1]",
            ),
            ("inline_math_wrapping_display_math", "$a $$b$$ c$", "$a $$b$$ c$"),
            ("triple_dollar_math", "$$$x$$$", "$$$x$$$"),
            ("inline_math_wrapping_display_math_and_code", "$a $$`b`$$ c$", "$a $$b$$ c$"),
            ("math_wrapping_inline_code", "$`total`$", "$total$"),
            ("math_containing_inline_code", "Math $`x` + 1$", "Math $x + 1$"),
            (
                "literal_private_use_char_survives_token_restore",
                "\ue000 `x` and $`y`$",
                "\ue000 x and $y$",
            ),
            ("inline_code_inner_spaces_preserved", "Use `a  b` here", "Use a  b here"),
            ("inline_code_inner_tab_preserved", "Run  `a\tb`  now", "Run a\tb now"),
            (
                "table_inline_code_inner_spaces_preserved",
                "| `a  b` | c   d |\n| --- | --- |",
                "a  b · c d",
            ),
            ("whitespace_only_inline_code_is_empty", "` `\n`  `", ""),
            ("lone_surrogates_replaced", "**hi** \udc00 there", "hi \ufffd there"),
        ]
        for label, raw, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(strip_markdown(raw), expected)

    def test_nested_math_and_code_never_leak_placeholders(self) -> None:
        for raw in (
            "$a $$b$$ c$",
            "$$$x$$$",
            "$a $$`b`$$ c$",
            "$`total`$",
            "Math $`x` + 1$",
            "| $a $$b$$ c$ | $$$x$$$ |",
        ):
            with self.subTest(raw=raw):
                stripped = strip_markdown(raw)
                self.assertFalse(
                    any("\ue000" <= char <= "\uf8ff" for char in stripped), repr(stripped)
                )

    def test_pathological_inputs_are_fast_and_bounded(self) -> None:
        patterns = [
            ("unclosed_links", "[a](" * 50_000),
            ("long_unclosed_paren", "[label](" + "a" * 100_000),
            ("nested_parens", "[label](" + "(a)" * 50_000),
            ("unclosed_bold", "**a " * 50_000),
            ("unclosed_italic", "*a " * 50_000),
            ("unclosed_strike", "~~a " * 50_000),
            ("unclosed_math", "$a " * 50_000),
            ("long_hr_miss", "- " * 50_000 + "x"),
            ("many_short_lines", "a\n" * 5_000),
            ("many_table_rows", "|a|b|\n" * 2_000),
            ("many_tool_markers", "🔧 `shell` [1]\n" * 5_000 + "**done**"),
            ("huge_reply_fallback", "> <@a:b> " + "x" * 100_000 + "\n\n**reply**"),
        ]
        for label, text in patterns:
            with self.subTest(case=label):
                start = time.monotonic()
                result = strip_markdown(text)
                elapsed = time.monotonic() - start
                self.assertLess(elapsed, 0.5, f"{label} took {elapsed:.3f}s")
                self.assertLessEqual(len(result), mindroom_pushkin.MAX_STRIP_CHARS)
        self.assertEqual(strip_markdown("🔧 `shell` [1]\n" * 5_000 + "**done**"), "done")
        self.assertEqual(
            strip_markdown("> <@a:b> " + "x" * 100_000 + "\n\n**reply**"),
            "reply",
        )

    def test_truncate_utf8(self) -> None:
        self.assertEqual(_truncate_utf8("hello", 10), "hello")
        self.assertEqual(_truncate_utf8("hello", 3), "hel")
        self.assertEqual(_truncate_utf8("hello", 0), "")
        self.assertEqual(_truncate_utf8("😀😀😀", 5), "😀")
        self.assertEqual(_truncate_utf8("😀😀😀", 8), "😀😀")
        self.assertEqual(_truncate_utf8("😀", 3), "")
        self.assertEqual(_truncate_utf8("café", 4), "caf")
        self.assertEqual(_truncate_utf8("café", 5), "café")
        # JSON-escaped quotes count as 2 bytes each (\")
        truncated_quotes = _truncate_utf8('"' * 300, 256)
        self.assertEqual(len(truncated_quotes), 128)
        self.assertLessEqual(
            len(json.dumps(truncated_quotes, ensure_ascii=False).encode("utf-8")) - 2,
            256,
        )

    def test_lone_surrogates_are_replaced_not_raised(self) -> None:
        self.assertEqual(_truncate_utf8("a\ud800b", 10), "a\ufffdb")
        self.assertEqual(_truncate_utf8("\udfff" * 4, 6), "\ufffd\ufffd")
        self.assertEqual(_truncate_utf8("@al\ud83dice:example.com", 256), "@al\ufffdice:example.com")
        self.assertEqual(_clean_display_field("Al\ud83dice\x01"), "Al\ufffdice")
        # A well-formed astral character is one code point, not a surrogate pair.
        self.assertEqual(_truncate_utf8("😀", 4), "😀")
        self.assertEqual(_clean_display_field("😀"), "😀")


class MindroomApnsPushkinTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now_ms = BASE_NOW_MS
        self.reactor = MemoryReactorClock()
        self.sent: Dict[str, List[NotificationRequest]] = {APP_ID: [], STOCK_APP_ID: []}
        self.send_handler: Callable[[NotificationRequest], NotificationResult] = (
            lambda req: NotificationResult(req.notification_id, "200")
        )

        def _make_apns(*args: Any, **kwargs: Any) -> MagicMock:
            topic = kwargs.get("topic", APP_ID)
            client = MagicMock()

            async def _send(req: NotificationRequest) -> NotificationResult:
                self.sent[topic].append(copy.deepcopy(req))
                return self.send_handler(req)

            client.send_notification.side_effect = _send
            return client

        patch("sygnal.apnspushkin.APNs", side_effect=_make_apns).start()
        patch("os.path.exists", lambda p: p == FAKE_KEYFILE).start()
        patch.object(mindroom_pushkin, "now_ms", side_effect=lambda: self.now_ms).start()
        self.addCleanup(patch.stopall)

        base_app = {
            "keyfile": FAKE_KEYFILE,
            "key_id": "KEYID12345",
            "team_id": "TEAMID1234",
            "platform": "production",
            "convert_device_token_to_hex": False,
        }
        cfg = merge_left_with_defaults(
            CONFIG_DEFAULTS,
            {
                "log": {"setup": {"version": 1, "disable_existing_loggers": False}},
                "apps": {
                    APP_ID: {**base_app, "type": "mindroom_pushkin.MindroomApnsPushkin", "topic": APP_ID},
                    STOCK_APP_ID: {**base_app, "type": "apns", "topic": STOCK_APP_ID},
                },
            },
        )
        self.sygnal = Sygnal(cfg, self.reactor)  # type: ignore[arg-type]
        self.pushkin = await self._await(self.sygnal._make_pushkin(APP_ID, cfg["apps"][APP_ID]))
        self.stock = await self._await(self.sygnal._make_pushkin(STOCK_APP_ID, cfg["apps"][STOCK_APP_ID]))
        self.assertIsInstance(self.pushkin, MindroomApnsPushkin)
        self.assertIs(type(self.stock), ApnsPushkin)

    async def _await(self, coro: Any, *, advance: Optional[List[int]] = None) -> Any:
        fut = ensureDeferred(coro).asFuture(asyncio.get_running_loop())
        for secs in advance or []:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if fut.done():
                break
            self.now_ms += secs * 1000
            self.reactor.advance(secs)
        return await fut

    async def _dispatch(
        self,
        notif: Notification,
        *,
        pushkin: Optional[ApnsPushkin] = None,
        advance: Optional[List[int]] = None,
    ) -> List[str]:
        ctx = NotificationContext(
            request_id="req-1",
            opentracing_span=self.sygnal.tracer.start_span("test"),
            start_time=0.0,
        )
        return await self._await(
            (pushkin or self.pushkin).dispatch_notification(notif, notif.devices[0], ctx),
            advance=advance,
        )

    async def _send(self, raw: Dict[str, Any], *, stock: bool = False) -> Optional[Dict[str, Any]]:
        """Dispatch ``raw`` and return the APNs message, or None if Sygnal found it too long."""
        raw = copy.deepcopy(raw)
        app_id = STOCK_APP_ID if stock else APP_ID
        raw["devices"][0]["app_id"] = app_id
        self.sent[app_id].clear()
        try:
            rejected = await self._dispatch(
                Notification(raw), pushkin=self.stock if stock else self.pushkin
            )
        except BodyTooLongException:
            return None
        self.assertEqual(rejected, [])
        self.assertEqual(len(self.sent[app_id]), 1)
        return self.sent[app_id][0].message

    def _stock_payload(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Return the untruncated payload stock ApnsPushkin builds for ``raw``."""
        notif = Notification(copy.deepcopy(raw))
        log = NotificationLoggerAdapter(logging.getLogger(__name__), {"request_id": "req-stock"})
        payload = self.stock._get_payload_full(notif, notif.devices[0], log, True)
        assert payload is not None
        return payload

    async def test_valid_rings(self) -> None:
        t = self.now_ms
        cases = [
            ("clear_audio", _raw(content=_ring(t - 2_000), tweaks={"sound": "ring"}, default_payload={"aps": {"sound": "default", "mutable-content": 1}}), "VOICE_CALL_FROM_USER", "Alice", t + 28_000, 28),
            ("hint_audio_named_room", _raw(event_type=ENCRYPTED_EVENT_TYPE, room_name="Small Group", content=_enc(_ring(t - 1_500)), tweaks={"sound": "ring"}), "VOICE_CALL_FROM_USER", "Alice", t + 28_500, 28),
            ("clear_video", _raw(sender_display_name="Test Caller", content=_ring(t - 1_000, intent="video"), tweaks={"sound": "ring"}), "VIDEO_CALL_FROM_USER", "Test Caller", t + 29_000, 29),
            ("hint_video", _raw(event_type=ENCRYPTED_EVENT_TYPE, sender_display_name="Test Caller", content=_enc(_ring(t - 1_000, intent="video")), tweaks={"sound": "ring"}), "VIDEO_CALL_FROM_USER", "Test Caller", t + 29_000, 29),
            ("omitted_intent_sender_fallback", _raw(sender="@bob:example.com", sender_display_name="", content=_ring(t, intent=None), tweaks={"sound": "ring"}), "VOICE_CALL_FROM_USER", "@bob:example.com", t + 30_000, 30),
            ("missing_sender_fallback", _raw(sender=None, sender_display_name=None, content=_ring(t, intent=None), tweaks={"sound": "ring"}), "VOICE_CALL_FROM_USER", " ", t + 30_000, 30),
            ("long_name_truncated", _raw(sender_display_name="A" * 2000, content=_ring(t), tweaks={"sound": "ring"}), "VOICE_CALL_FROM_USER", "A" * self.pushkin.MAX_FIELD_LENGTH, t + 30_000, 30),
            ("low_prio_override_and_life_cap", _raw(prio="low", content=_ring(t, lifetime=600_000), tweaks={"sound": "ring"}), "VOICE_CALL_FROM_USER", "Alice", t + MAX_RING_LIFETIME_MS, 120),
            ("clock_skew_boundary", _raw(content=_ring(t + MAX_CLOCK_SKEW_MS), tweaks={"sound": "ring"}), "VOICE_CALL_FROM_USER", "Alice", t + MAX_CLOCK_SKEW_MS + 30_000, 50),
        ]
        for label, raw, loc_key, name, exp, ttl in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                self.assertEqual(len(self.sent[APP_ID]), 1)
                req = self.sent[APP_ID][0]
                self.assertEqual(req.priority, 10)
                self.assertEqual(req.time_to_live, ttl)
                self.assertEqual(req.message["aps"]["alert"], {"loc-key": loc_key, "loc-args": [name]})
                self.assertEqual(req.message["aps"]["sound"], RING_SOUND)
                self.assertEqual(req.message[RING_PAYLOAD_KEY], {"expires_at": exp})
                self.assertNotIn("thread-id", req.message["aps"])
                self.assertNotIn("sender", req.message)
                self.assertNotIn("sender_display_name", req.message)
                self.assertNotIn("room_name", req.message)

    async def test_badge_only_and_event_id_only_match_stock(self) -> None:
        rt = {"sound": "ring"}
        cases: List[Tuple[str, Dict[str, Any]]] = [
            ("badge_only", {"type": None, "sender": "", "counts": {"unread": 2}, "devices": [_dev(tweaks=rt)]}),
            (
                "badge_only_with_default_payload",
                {
                    "type": None,
                    "sender": "@alice:example.com",
                    "room_id": "!room:example.com",
                    "counts": {"unread": 5},
                    "devices": [_dev(tweaks=rt, default_payload={"aps": {"sound": "default"}})],
                },
            ),
            (
                "event_id_only",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$e1",
                    "prio": "high",
                    "counts": {"unread": 1},
                    "devices": [_dev(default_payload={"aps": {"mutable-content": 1}})],
                },
            ),
            (
                "caller_ring_field_event_id_only",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$e2",
                    "prio": "low",
                    "counts": {"unread": 3},
                    "devices": [_dev(tweaks=rt, default_payload={"aps": {"mutable-content": 1}, RING_PAYLOAD_KEY: {"expires_at": 1}})],
                },
            ),
            (
                "member_join_with_empty_default_alert_stays_badge_only",
                _raw(
                    event_type="m.room.member",
                    membership="join",
                    user_is_target=False,
                    default_payload={"aps": {"alert": {}}},
                ),
            ),
            (
                "member_join_with_custom_default_alert_stays_badge_only",
                _raw(
                    event_type="m.room.member",
                    membership="join",
                    user_is_target=False,
                    default_payload={"aps": {"alert": {"loc-key": "CUSTOM"}}},
                ),
            ),
            (
                "member_invite_not_target_with_default_alert_stays_badge_only",
                _raw(
                    event_type="m.room.member",
                    membership="invite",
                    user_is_target=False,
                    default_payload={"aps": {"alert": {}}},
                ),
            ),
            (
                "member_join_with_reserved_fields_stays_stock",
                _raw(
                    event_type="m.room.member",
                    membership="join",
                    user_is_target=False,
                    default_payload=SPOOFED_DEFAULT_PAYLOAD,
                ),
            ),
            (
                "event_id_only_with_reserved_fields_stays_stock",
                {
                    "room_id": "!room:example.com",
                    "event_id": "$e3",
                    "prio": "high",
                    "counts": {"unread": 1},
                    "devices": [_dev(default_payload=SPOOFED_DEFAULT_PAYLOAD)],
                },
            ),
        ]
        for label, raw in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.sent[STOCK_APP_ID].clear()
                raw_stock = copy.deepcopy(raw)
                raw_stock["devices"][0]["app_id"] = STOCK_APP_ID

                rej_m = await self._dispatch(Notification(copy.deepcopy(raw)))
                rej_s = await self._dispatch(Notification(raw_stock), pushkin=self.stock)
                self.assertEqual(rej_m, rej_s)
                self.assertEqual(len(self.sent[APP_ID]), 1)
                self.assertEqual(len(self.sent[STOCK_APP_ID]), 1)
                got, want = self.sent[APP_ID][0], self.sent[STOCK_APP_ID][0]
                self.assertEqual(
                    (got.message, got.priority, got.time_to_live),
                    (want.message, want.priority, want.time_to_live),
                )

    async def test_invalid_rings_never_get_ring_behavior(self) -> None:
        t = self.now_ms
        valid = _ring(t)
        rt = {"sound": "ring"}
        dp = {"aps": {"sound": "default"}}
        cases: List[Tuple[str, Dict[str, Any]]] = [
            ("msg_low_prio", _raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "Hi"}, prio="low")),
            ("encrypted_no_hint", _raw(event_type=ENCRYPTED_EVENT_TYPE, content={"algorithm": "m.megolm.v1.aes-sha2"}, tweaks=rt, default_payload=dp)),
            ("group_notif_clear", _raw(content={**valid, "notification_type": "notification"}, tweaks=rt, default_payload=dp)),
            ("group_notif_hint", _raw(event_type=ENCRYPTED_EVENT_TYPE, content=_enc({**valid, "notification_type": "notification"}), tweaks=rt, default_payload=dp)),
            ("legacy_invite_audio", _raw(event_type="m.call.invite", content={**valid, "offer": {"sdp": "v=0\r\nm=audio 9\r\n"}}, tweaks=rt, default_payload=dp)),
            ("legacy_invite_video", _raw(event_type="m.call.invite", content={**valid, "offer": {"sdp": "v=0\r\nm=video 9\r\n"}}, tweaks=rt, default_payload=dp)),
            ("wrong_wire_msg_top", _raw(event_type="m.room.message", content={**valid, "msgtype": "m.text", "body": "hi"}, tweaks=rt, default_payload=dp)),
            ("wrong_wire_msg_hint", _raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "hi", RING_HINT_KEY: valid}, tweaks=rt, default_payload=dp)),
            ("wrong_wire_rtc_nested", _raw(content={RING_HINT_KEY: valid}, tweaks=rt, default_payload=dp)),
            ("wrong_wire_enc_top", _raw(event_type=ENCRYPTED_EVENT_TYPE, content={**valid, "algorithm": "m.megolm.v1.aes-sha2"}, tweaks=rt, default_payload=dp)),
            ("wrong_wire_unknown", _raw(event_type="com.example.custom", content=valid, tweaks=rt, default_payload=dp)),
            ("no_tweak", _raw(content=valid, tweaks=None, default_payload=dp)),
            ("other_tweak", _raw(content=valid, tweaks={"sound": "default"}, default_payload=dp)),
            ("malformed_hint", _raw(event_type=ENCRYPTED_EVENT_TYPE, content=_enc("not-a-dict"), tweaks=rt)),
            ("missing_content", _raw(content=None, tweaks=rt)),
            ("caller_ring_field_full", _raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "Hi"}, prio="low", default_payload={**dp, RING_PAYLOAD_KEY: {"expires_at": 1}})),
        ]
        bad_ts = [
            ("missing_ts", {"notification_type": "ring", "lifetime": 30_000}),
            ("missing_life", {"notification_type": "ring", "sender_ts": t}),
            ("none_ts", {"notification_type": "ring", "sender_ts": None, "lifetime": 30_000}),
            ("str_ts", {"notification_type": "ring", "sender_ts": str(t), "lifetime": 30_000}),
            ("float_ts", {"notification_type": "ring", "sender_ts": float(t), "lifetime": 30_000}),
            ("bool_ts", {"notification_type": "ring", "sender_ts": True, "lifetime": 30_000}),
            ("bool_life", {"notification_type": "ring", "sender_ts": t, "lifetime": True}),
            ("zero_ts", {"notification_type": "ring", "sender_ts": 0, "lifetime": 30_000}),
            ("neg_ts", {"notification_type": "ring", "sender_ts": -1_000, "lifetime": 30_000}),
            ("zero_life", {"notification_type": "ring", "sender_ts": t, "lifetime": 0}),
            ("neg_life", {"notification_type": "ring", "sender_ts": t, "lifetime": -5_000}),
            ("far_future", {"notification_type": "ring", "sender_ts": t + MAX_CLOCK_SKEW_MS + 1, "lifetime": 30_000}),
        ]
        cases.extend((f"bad_ts_{k}", _raw(content=c, tweaks=rt, default_payload=dp)) for k, c in bad_ts)

        for label, raw in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.sent[STOCK_APP_ID].clear()
                raw_stock = copy.deepcopy(raw)
                raw_stock["devices"][0]["app_id"] = STOCK_APP_ID

                rej_m = await self._dispatch(Notification(copy.deepcopy(raw)))
                rej_s = await self._dispatch(Notification(raw_stock), pushkin=self.stock)
                self.assertEqual(rej_m, rej_s)
                self.assertEqual(len(self.sent[APP_ID]), 1)
                self.assertEqual(len(self.sent[STOCK_APP_ID]), 1)
                got, want = self.sent[APP_ID][0], self.sent[STOCK_APP_ID][0]
                self.assertEqual(got.priority, want.priority)
                self.assertEqual(got.time_to_live, want.time_to_live)
                self.assertIsNone(got.time_to_live)
                self.assertEqual(got.message["aps"].get("sound"), want.message["aps"].get("sound"))
                self.assertNotEqual(got.message["aps"].get("sound"), RING_SOUND)
                # Only a validated ring may carry mindroom_ring: a caller-supplied one is
                # stripped (unlike stock) so the iOS extension never treats a message as a ring.
                self.assertNotIn(RING_PAYLOAD_KEY, got.message)
                if label == "caller_ring_field_full":
                    self.assertEqual(want.message[RING_PAYLOAD_KEY], {"expires_at": 1})
                self.assertEqual(got.message["aps"]["mutable-content"], 1)
                self.assertEqual(got.message["aps"]["thread-id"], "!room:example.com")
                self.assertEqual(got.message["sender"], "@alice:example.com")
                self.assertEqual(got.message["sender_display_name"], "Alice")

    async def test_rich_text_and_notice_alerts(self) -> None:
        cases = [
            (
                "text_in_named_room",
                _raw(
                    event_type="m.room.message",
                    room_name="Engineering",
                    content={"msgtype": "m.text", "body": "## Update\n**Deploy** succeeded with `v2`!"},
                    default_payload={"aps": {"sound": "default"}},
                ),
                {"title": "Alice", "subtitle": "Engineering", "body": "Update\nDeploy succeeded with v2!"},
                "Engineering",
            ),
            (
                "notice_with_room_alias",
                _raw(
                    event_type="m.room.message",
                    room_alias="#ops:example.com",
                    content={"msgtype": "m.notice", "body": "🔧 `shell` [1]\nAll checks **passed**"},
                ),
                {"title": "Alice", "subtitle": "#ops:example.com", "body": "All checks passed"},
                "#ops:example.com",
            ),
            (
                "dm_duplicate_subtitle_suppressed",
                _raw(
                    event_type="m.room.message",
                    sender_display_name="Alice",
                    room_name="Alice",
                    content={"msgtype": "m.text", "body": "Hey *there*!"},
                ),
                {"title": "Alice", "body": "Hey there!"},
                "Alice",
            ),
            (
                "no_room_name",
                _raw(
                    event_type="m.room.message",
                    content={"msgtype": "m.text", "body": "Direct message"},
                ),
                {"title": "Alice", "body": "Direct message"},
                None,
            ),
            (
                "sender_id_fallback_when_no_display_name",
                _raw(
                    event_type="m.room.message",
                    sender="@bob:example.com",
                    sender_display_name="",
                    room_name="General",
                    content={"msgtype": "m.text", "body": "Hi"},
                ),
                {"title": "@bob:example.com", "subtitle": "General", "body": "Hi"},
                "General",
            ),
            (
                "space_fallback_when_no_sender",
                _raw(
                    event_type="m.room.message",
                    sender=None,
                    sender_display_name=None,
                    content={"msgtype": "m.text", "body": "Anon"},
                ),
                {"title": " ", "body": "Anon"},
                None,
            ),
        ]
        for label, raw, expected_alert, expected_room_name in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                self.assertEqual(len(self.sent[APP_ID]), 1)
                msg = self.sent[APP_ID][0].message
                self.assertEqual(msg["aps"]["alert"], expected_alert)
                self.assertEqual(msg[PLAIN_PAYLOAD_KEY], 1)
                self.assertEqual(msg["aps"]["mutable-content"], 1)
                self.assertEqual(msg["aps"]["thread-id"], "!room:example.com")
                if expected_room_name is not None:
                    self.assertEqual(msg["room_name"], expected_room_name)
                else:
                    self.assertNotIn("room_name", msg)

    async def test_empty_after_strip_fallback(self) -> None:
        cases = [
            (
                "tool_markers_only_in_room",
                _raw(
                    event_type="m.room.message",
                    room_name="Ops",
                    content={"msgtype": "m.text", "body": "🔧 `shell` [1] ⏳\n🔧 `read` [2]"},
                ),
                {"loc-key": "MSG_FROM_USER_IN_ROOM", "loc-args": ["Alice", "Ops"]},
            ),
            (
                "whitespace_only_no_room",
                _raw(
                    event_type="m.room.message",
                    content={"msgtype": "m.text", "body": "   \n\t  "},
                ),
                {"loc-key": "MSG_FROM_USER", "loc-args": ["Alice"]},
            ),
            (
                "notice_hr_only_in_room",
                _raw(
                    event_type="m.room.message",
                    room_name="Ops",
                    content={"msgtype": "m.notice", "body": "---"},
                ),
                {"loc-key": "MSG_FROM_USER_IN_ROOM", "loc-args": ["Alice", "Ops"]},
            ),
            (
                "emote_empty_after_strip",
                _raw(
                    event_type="m.room.message",
                    room_name="Ops",
                    content={"msgtype": "m.emote", "body": "🔧 `shell` [1]"},
                ),
                {"loc-key": "MSG_FROM_USER_IN_ROOM", "loc-args": ["Alice", "Ops"]},
            ),
        ]
        for label, raw, expected_alert in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                self.assertEqual(len(self.sent[APP_ID]), 1)
                msg = self.sent[APP_ID][0].message
                self.assertEqual(msg["aps"]["alert"], expected_alert)
                self.assertNotIn(PLAIN_PAYLOAD_KEY, msg)
                self.assertEqual(msg["aps"]["mutable-content"], 1)
                self.assertEqual(msg["aps"]["thread-id"], "!room:example.com")
                self.assertEqual(msg["sender"], "@alice:example.com")

    async def test_emote_markdown_stripping(self) -> None:
        cases = [
            (
                "emote_in_room",
                _raw(
                    event_type="m.room.message",
                    room_name="Lounge",
                    content={"msgtype": "m.emote", "body": "**waves** at [everyone](https://example.com)"},
                ),
                {"loc-key": "ACTION_FROM_USER_IN_ROOM", "loc-args": ["Lounge", "Alice", "waves at everyone"]},
            ),
            (
                "emote_no_room",
                _raw(
                    event_type="m.room.message",
                    content={"msgtype": "m.emote", "body": "celebrates `v1.0`!"},
                ),
                {"loc-key": "ACTION_FROM_USER", "loc-args": ["Alice", "celebrates v1.0!"]},
            ),
            (
                # Stripping this a second time on the device would eat the asterisks.
                "emote_inline_code_kept_literal",
                _raw(
                    event_type="m.room.message",
                    content={"msgtype": "m.emote", "body": "tests `**kwargs**`"},
                ),
                {"loc-key": "ACTION_FROM_USER", "loc-args": ["Alice", "tests **kwargs**"]},
            ),
        ]
        for label, raw, expected_alert in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                msg = self.sent[APP_ID][0].message
                self.assertEqual(msg["aps"]["alert"], expected_alert)
                self.assertEqual(msg[PLAIN_PAYLOAD_KEY], 1)
                self.assertEqual(msg["aps"]["mutable-content"], 1)
                self.assertEqual(msg["aps"]["thread-id"], "!room:example.com")
                self.assertEqual(msg["sender"], "@alice:example.com")

        # Forced to the stock fallback, the action text is neither stripped nor marked.
        def _emote(pad: int) -> Dict[str, Any]:
            return _raw(
                event_type="m.room.message",
                content={"msgtype": "m.emote", "body": "tests `**kwargs**`"},
                default_payload={"pad": "x" * pad},
            )

        raw = _emote(4096 - _json_size(self._stock_payload(_emote(0))))
        want = await self._send(raw, stock=True)
        got = await self._send(raw)
        assert want is not None and got is not None
        self.assertEqual(_json_size(want), 4096)
        self.assertEqual(got, want)
        self.assertEqual(got["aps"]["alert"]["loc-args"], ["Alice", "tests `**kwargs**`"])
        self.assertNotIn(PLAIN_PAYLOAD_KEY, got)

    async def test_internal_and_redaction_events_are_dropped(self) -> None:
        oversized = {"aps": {"sound": "default"}, "pad": "x" * 5000}
        cases: List[Tuple[str, str, Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = [
            ("active_runs", "io.mindroom.active_runs", {"runs": []}, None, None),
            ("background_jobs", "io.mindroom.background_jobs", {"jobs": []}, None, None),
            ("scheduled_task", "com.mindroom.scheduled.task", {"task_id": "t1"}, None, None),
            ("tool_approval_response", "io.mindroom.tool_approval_response", {"decision": "approve"}, None, None),
            ("redaction", REDACTION_EVENT_TYPE, {"redacts": "$evt0"}, {"sound": "ring"}, oversized),
        ]
        log = NotificationLoggerAdapter(logging.getLogger(__name__), {"request_id": "req-drop"})
        for label, event_type, content, tweaks, default_payload in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                notif = Notification(
                    _raw(
                        event_type=event_type,
                        room_name="Ops",
                        content=content,
                        tweaks=tweaks,
                        default_payload=default_payload,
                    )
                )
                self.assertIsNone(self.pushkin._get_payload_full(notif, notif.devices[0], log, True))
                self.assertEqual(await self._dispatch(notif), [])
                self.assertEqual(self.sent[APP_ID], [])

    async def test_non_pending_tool_approval_is_dropped(self) -> None:
        cases: List[Tuple[str, Any]] = [
            ("approved", {"status": "approved", "tool_name": "shell"}),
            ("denied", {"status": "denied", "tool_name": "shell"}),
            ("expired", {"status": "expired", "tool_name": "shell"}),
            ("missing_status", {"tool_name": "shell"}),
            ("missing_content", None),
            ("non_dict_content", "pending"),
        ]
        log = NotificationLoggerAdapter(logging.getLogger(__name__), {"request_id": "req-approval-drop"})
        for label, content in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                raw = _raw(event_type=TOOL_APPROVAL_TYPE, room_name="Ops")
                if content is not None:
                    raw["content"] = content
                notif = Notification(raw)
                self.assertIsNone(self.pushkin._get_payload_full(notif, notif.devices[0], log, True))
                self.assertEqual(await self._dispatch(notif), [])
                self.assertEqual(self.sent[APP_ID], [])

    async def test_pending_tool_approval_alerts(self) -> None:
        cases = [
            (
                "named_room_with_tool_name",
                _raw(
                    event_type=TOOL_APPROVAL_TYPE,
                    room_name="Ops",
                    content={"status": "pending", "tool_name": "run_shell"},
                ),
                {"title": "Alice", "subtitle": "Ops", "body": "Approval needed: run_shell"},
                "Ops",
            ),
            (
                "dm_same_room_name_suppresses_subtitle",
                _raw(
                    event_type=TOOL_APPROVAL_TYPE,
                    sender_display_name="Alice",
                    room_name="Alice",
                    content={"status": "pending", "tool_name": "read_file"},
                ),
                {"title": "Alice", "body": "Approval needed: read_file"},
                "Alice",
            ),
            (
                "no_room_and_missing_tool_name",
                _raw(
                    event_type=TOOL_APPROVAL_TYPE,
                    content={"status": "pending"},
                ),
                {"title": "Alice", "body": "Approval needed"},
                None,
            ),
            (
                "sanitizes_control_chars_and_surrogates",
                _raw(
                    event_type=TOOL_APPROVAL_TYPE,
                    room_name="Ops",
                    content={"status": "pending", "tool_name": "run\x01tool\ud83d\n"},
                    default_payload=SPOOFED_DEFAULT_PAYLOAD,
                ),
                {"title": "Alice", "subtitle": "Ops", "body": "Approval needed: run tool\ufffd"},
                "Ops",
            ),
            (
                "blank_tool_name_falls_back",
                _raw(
                    event_type=TOOL_APPROVAL_TYPE,
                    content={"status": "pending", "tool_name": " \x01\t "},
                ),
                {"title": "Alice", "body": "Approval needed"},
                None,
            ),
        ]
        for label, raw, expected_alert, expected_room_name in cases:
            with self.subTest(case=label):
                msg = await self._send(raw)
                assert msg is not None
                encoded = json.dumps(msg, ensure_ascii=False).encode("utf-8")
                self.assertLessEqual(len(encoded), 4096)
                self.assertNotIn("spoof", encoded.decode("utf-8").lower())
                self.assertEqual(msg["aps"]["alert"], expected_alert)
                self.assertEqual(msg[PLAIN_PAYLOAD_KEY], 1)
                self.assertEqual(msg["aps"]["mutable-content"], 1)
                self.assertEqual(msg["aps"]["thread-id"], "!room:example.com")
                self.assertEqual(msg["sender"], "@alice:example.com")
                self.assertEqual(msg["sender_display_name"], "Alice")
                self.assertEqual(msg.get("room_name"), expected_room_name)

    async def test_tool_approval_oversized_and_tight_budget_fits_within_4096_bytes(self) -> None:
        oversized = _raw(
            event_type=TOOL_APPROVAL_TYPE,
            sender="@" + "a" * 400 + ":example.com",
            sender_display_name="😀" * 500,
            room_name="🔥" * 500,
            room_id="!" + "b" * 400 + ":example.com",
            content={"status": "pending", "tool_name": "🔧" * 500},
        )
        msg = await self._send(oversized)
        assert msg is not None
        self.assertLessEqual(_json_size(msg), 4096)
        alert = msg["aps"]["alert"]
        self.assertTrue(alert["body"].startswith("Approval needed: "))
        self.assertLessEqual(len(alert["body"].encode("utf-8")), MAX_TOOL_APPROVAL_BODY_BYTES)
        self.assertLessEqual(len(alert["title"].encode("utf-8")), MAX_ALERT_TITLE_BYTES)
        self.assertLessEqual(len(alert["subtitle"].encode("utf-8")), MAX_ALERT_SUBTITLE_BYTES)
        self.assertLessEqual(len(msg["sender"].encode("utf-8")), MAX_SENDER_BYTES)
        self.assertLessEqual(len(msg["room_name"].encode("utf-8")), MAX_ROOM_NAME_BYTES)
        self.assertEqual(msg[PLAIN_PAYLOAD_KEY], 1)
        self.assertEqual(msg["aps"]["mutable-content"], 1)

        # Under tight default_payload budgets, shave cleanly across rich -> reduced -> stock.
        for pad, expected_stage in ((3650, "rich"), (3750, "reduced"), (3900, "stock")):
            with self.subTest(pad=pad, stage=expected_stage):
                raw = _raw(
                    event_type=TOOL_APPROVAL_TYPE,
                    room_name="Engineering Room",
                    content={"status": "pending", "tool_name": "run_shell_command_" + "x" * 100},
                    default_payload={"pad": "x" * pad},
                )
                want = await self._send(raw, stock=True)
                got = await self._send(raw)
                assert want is not None and got is not None
                self.assertLessEqual(_json_size(got), 4096)
                got_alert = got["aps"]["alert"]
                stage = (
                    "stock"
                    if "loc-key" in got_alert
                    else ("rich" if "room_name" in got else "reduced")
                )
                self.assertEqual(stage, expected_stage)
                if stage != "stock":
                    self.assertTrue(got_alert["body"].startswith("Approval needed: "))

    async def test_non_text_alerts_receive_extension_metadata(self) -> None:
        cases = [
            (
                "encrypted_room",
                _raw(
                    event_type=ENCRYPTED_EVENT_TYPE,
                    room_name="Secret Room",
                    content={"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"},
                ),
                {"loc-key": "MSG_FROM_USER_IN_ROOM", "loc-args": ["Alice", "Secret Room"]},
                "Secret Room",
            ),
            (
                "image_message",
                _raw(
                    event_type="m.room.message",
                    room_name="Photos",
                    content={"msgtype": "m.image", "body": "sunset.jpg"},
                ),
                {"loc-key": "IMAGE_FROM_USER_IN_ROOM", "loc-args": ["Alice", "sunset.jpg", "Photos"]},
                "Photos",
            ),
            (
                "room_invite",
                _raw(
                    event_type="m.room.member",
                    room_name="Design",
                    user_is_target=True,
                    membership="invite",
                ),
                {"loc-key": "USER_INVITE_TO_NAMED_ROOM", "loc-args": ["Alice", "Design"]},
                "Design",
            ),
        ]
        for label, raw, expected_alert, expected_room in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                msg = self.sent[APP_ID][0].message
                self.assertEqual(msg["aps"]["alert"], expected_alert)
                self.assertEqual(msg["aps"]["mutable-content"], 1)
                self.assertEqual(msg["aps"]["thread-id"], "!room:example.com")
                self.assertEqual(msg["sender"], "@alice:example.com")
                self.assertEqual(msg["sender_display_name"], "Alice")
                self.assertEqual(msg["room_name"], expected_room)
                self.assertNotIn(PLAIN_PAYLOAD_KEY, msg)

    async def test_oversized_multibyte_utf8_fits_within_4096_bytes(self) -> None:
        big_sender_name = "😀" * 500
        big_room_name = "🔥" * 500
        big_sender_id = "@" + "a" * 400 + ":example.com"
        big_room_id = "!" + "b" * 400 + ":example.com"
        cases = [
            (
                "long_multibyte_body",
                _raw(
                    event_type="m.room.message",
                    sender=big_sender_id,
                    sender_display_name=big_sender_name,
                    room_name=big_room_name,
                    room_id=big_room_id,
                    content={"msgtype": "m.text", "body": "**Hello** " + "🌍" * 2000},
                ),
            ),
            (
                "short_body_with_huge_names",
                _raw(
                    event_type="m.room.message",
                    sender=big_sender_id,
                    sender_display_name=big_sender_name,
                    room_name=big_room_name,
                    room_id=big_room_id,
                    content={"msgtype": "m.text", "body": "ok"},
                ),
            ),
            (
                "encrypted_with_huge_names",
                _raw(
                    event_type=ENCRYPTED_EVENT_TYPE,
                    sender=big_sender_id,
                    sender_display_name=big_sender_name,
                    room_name=big_room_name,
                    room_id=big_room_id,
                    content={"algorithm": "m.megolm.v1.aes-sha2"},
                ),
            ),
        ]
        for label, raw in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                self.assertEqual(len(self.sent[APP_ID]), 1)
                msg = self.sent[APP_ID][0].message
                encoded = json.dumps(msg, ensure_ascii=False).encode("utf-8")
                self.assertLessEqual(len(encoded), 4096)
                self.assertLessEqual(len(msg["aps"]["thread-id"].encode("utf-8")), MAX_THREAD_ID_BYTES)
                self.assertLessEqual(len(msg["sender"].encode("utf-8")), MAX_SENDER_BYTES)
                self.assertLessEqual(
                    len(msg["sender_display_name"].encode("utf-8")),
                    MAX_SENDER_DISPLAY_NAME_BYTES,
                )
                self.assertLessEqual(len(msg["room_name"].encode("utf-8")), MAX_ROOM_NAME_BYTES)
                alert = msg["aps"]["alert"]
                if "title" in alert:
                    self.assertLessEqual(len(alert["title"].encode("utf-8")), MAX_ALERT_TITLE_BYTES)
                if "subtitle" in alert:
                    self.assertLessEqual(len(alert["subtitle"].encode("utf-8")), MAX_ALERT_SUBTITLE_BYTES)

    async def test_large_default_payload_and_control_char_names_never_regress(self) -> None:
        # 1. Large default_payload.custom_data (3500B & 3850B) must never raise BodyTooLongException
        #    when stock ApnsPushkin can deliver the notification.
        for pad_size in (3500, 3850):
            with self.subTest(custom_data_bytes=pad_size):
                self.sent[APP_ID].clear()
                self.sent[STOCK_APP_ID].clear()
                raw = _raw(
                    event_type="m.room.message",
                    room_name="Engineering Room",
                    content={"msgtype": "m.text", "body": "**hello**"},
                    default_payload={"custom_data": "x" * pad_size},
                )
                raw_stock = copy.deepcopy(raw)
                raw_stock["devices"][0]["app_id"] = STOCK_APP_ID

                self.assertEqual(await self._dispatch(Notification(copy.deepcopy(raw))), [])
                self.assertEqual(await self._dispatch(Notification(raw_stock), pushkin=self.stock), [])
                self.assertEqual(len(self.sent[APP_ID]), 1)
                self.assertEqual(len(self.sent[STOCK_APP_ID]), 1)
                msg = self.sent[APP_ID][0].message
                self.assertLessEqual(len(json.dumps(msg, ensure_ascii=False).encode("utf-8")), 4096)

        # 2. Control characters and JSON-escaped quotes in names must fit within 4096 bytes.
        name_cases = [
            ("only_control_chars", "\x01" * 300, "\x02" * 300),
            ("interleaved_control_chars", "A\x01" * 300, "B\x02" * 300),
            ("quotes_in_names", '"' * 300, '"' * 300),
        ]
        for label, sname, rname in name_cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                raw = _raw(
                    event_type="m.room.message",
                    sender_display_name=sname,
                    room_name=rname,
                    content={"msgtype": "m.text", "body": "Hello **world**"},
                )
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                self.assertEqual(len(self.sent[APP_ID]), 1)
                msg = self.sent[APP_ID][0].message
                encoded = json.dumps(msg, ensure_ascii=False).encode("utf-8")
                self.assertLessEqual(len(encoded), 4096)
                # Control chars must be sanitized out of alert/metadata fields.
                self.assertNotIn("\x01", msg["aps"]["alert"].get("title", ""))
                self.assertNotIn("\x02", msg["aps"]["alert"].get("subtitle", ""))
                self.assertNotIn("\x01", msg.get("sender_display_name", ""))
                self.assertNotIn("\x02", msg.get("room_name", ""))

    async def test_default_payload_spoofing_defense(self) -> None:
        spoofed = {
            "sender": "@spoof:evil.example",
            "sender_display_name": "Spoofed Admin",
            "room_name": "Spoofed Room",
            "aps": {
                "mutable-content": 0,
                "thread-id": "!spoof:evil.example",
                "sound": "default",
            },
        }
        # 1. Present fields overwrite spoofed values.
        self.sent[APP_ID].clear()
        notif_present = Notification(
            _raw(
                event_type="m.room.message",
                sender="@real:example.com",
                sender_display_name="Real User",
                room_name="Real Room",
                room_id="!real:example.com",
                content={"msgtype": "m.text", "body": "Hello"},
                default_payload=spoofed,
            )
        )
        self.assertEqual(await self._dispatch(notif_present), [])
        msg = self.sent[APP_ID][0].message
        self.assertEqual(msg["sender"], "@real:example.com")
        self.assertEqual(msg["sender_display_name"], "Real User")
        self.assertEqual(msg["room_name"], "Real Room")
        self.assertEqual(msg["aps"]["thread-id"], "!real:example.com")
        self.assertEqual(msg["aps"]["mutable-content"], 1)
        self.assertEqual(msg["aps"]["sound"], "default")

        # 2. Absent fields clear spoofed values instead of leaking them.
        self.sent[APP_ID].clear()
        notif_absent = Notification(
            _raw(
                event_type="m.room.message",
                sender=None,
                sender_display_name=None,
                room_name=None,
                room_id=None,
                content={"msgtype": "m.text", "body": "Hello"},
                default_payload=spoofed,
            )
        )
        self.assertEqual(await self._dispatch(notif_absent), [])
        msg_absent = self.sent[APP_ID][0].message
        self.assertNotIn("sender", msg_absent)
        self.assertNotIn("sender_display_name", msg_absent)
        self.assertNotIn("room_name", msg_absent)
        self.assertNotIn("thread-id", msg_absent["aps"])
        self.assertEqual(msg_absent["aps"]["mutable-content"], 1)

    async def test_reserved_fields_sanitized_on_every_event_alert(self) -> None:
        # (label, event type, content, extra _raw kwargs, alert kind), where the kind is
        # "structured" (stripped title/body alert), "stripped_emote" or "localized".
        events: List[Tuple[str, str, Optional[Dict[str, Any]], Dict[str, Any], str]] = [
            ("text", "m.room.message", {"msgtype": "m.text", "body": "**Hello**"}, {}, "structured"),
            ("notice", "m.room.message", {"msgtype": "m.notice", "body": "**Hello**"}, {}, "structured"),
            ("text_empty_after_strip", "m.room.message", {"msgtype": "m.text", "body": "---"}, {}, "localized"),
            ("emote", "m.room.message", {"msgtype": "m.emote", "body": "**waves**"}, {}, "stripped_emote"),
            ("emote_empty_after_strip", "m.room.message", {"msgtype": "m.emote", "body": "---"}, {}, "localized"),
            ("image", "m.room.message", {"msgtype": "m.image", "body": "sunset.jpg"}, {}, "localized"),
            ("encrypted", ENCRYPTED_EVENT_TYPE, {"algorithm": "m.megolm.v1.aes-sha2"}, {}, "localized"),
            ("invite", "m.room.member", None, {"user_is_target": True, "membership": "invite"}, "localized"),
            ("legacy_call_invite", "m.call.invite", {"offer": {"sdp": "v=0\r\nm=audio 9\r\n"}}, {}, "localized"),
            ("unknown_type", "com.example.custom", {"x": 1}, {}, "localized"),
        ]
        identities: List[Tuple[str, Dict[str, Any]]] = [
            ("present", REAL_IDENTITY),
            ("absent", {key: None for key in REAL_IDENTITY}),
        ]
        for label, event_type, content, extra, kind in events:
            for identity_label, identity in identities:
                with self.subTest(case=label, identity=identity_label):
                    msg = await self._send(
                        _raw(
                            event_type=event_type,
                            content=content,
                            default_payload=SPOOFED_DEFAULT_PAYLOAD,
                            **identity,
                            **extra,
                        )
                    )
                    assert msg is not None
                    self.assertNotIn("spoof", json.dumps(msg).lower())
                    self.assertNotIn(RING_PAYLOAD_KEY, msg)
                    self.assertEqual(msg.get(PLAIN_PAYLOAD_KEY), None if kind == "localized" else 1)
                    self.assertEqual(msg["custom"], "kept")
                    self.assertEqual(msg.get("room_id"), identity["room_id"])
                    self.assertEqual(msg.get("sender"), identity["sender"])
                    self.assertEqual(msg.get("sender_display_name"), identity["sender_display_name"])
                    self.assertEqual(msg.get("room_name"), identity["room_name"])
                    aps = msg["aps"]
                    self.assertEqual(aps["sound"], "default")
                    self.assertEqual(aps["mutable-content"], 1)
                    self.assertEqual(aps.get("thread-id"), identity["room_id"])
                    alert = aps["alert"]
                    if kind == "structured":
                        self.assertEqual(alert["title"], identity["sender_display_name"] or " ")
                        self.assertEqual(alert.get("subtitle"), identity["room_name"])
                        self.assertEqual(alert["body"], "Hello")
                    else:
                        self.assertIn("loc-key", alert)
                        self.assertNotIn("body", alert)
                        self.assertNotIn("title", alert)
                        self.assertNotIn("subtitle", alert)
                    if kind == "stripped_emote":
                        self.assertEqual(alert["loc-args"][-1], "waves")

    async def test_caller_alert_body_and_room_id_are_not_trusted(self) -> None:
        default_payload = {
            "room_id": "!spoof:evil.example",
            "aps": {"alert": {"body": "Spoofed body"}},
        }
        events: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = [
            (
                "image",
                "m.room.message",
                {"msgtype": "m.image", "body": "sunset.jpg"},
                {"loc-key": "IMAGE_FROM_USER_IN_ROOM", "loc-args": ["Alice", "sunset.jpg", "Photos"]},
            ),
            (
                "encrypted",
                ENCRYPTED_EVENT_TYPE,
                {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"},
                {"loc-key": "MSG_FROM_USER_IN_ROOM", "loc-args": ["Alice", "Photos"]},
            ),
            (
                "emote",
                "m.room.message",
                {"msgtype": "m.emote", "body": "**waves**"},
                {"loc-key": "ACTION_FROM_USER_IN_ROOM", "loc-args": ["Photos", "Alice", "waves"]},
            ),
            (
                "text",
                "m.room.message",
                {"msgtype": "m.text", "body": "**Hello**"},
                {"title": "Alice", "subtitle": "Photos", "body": "Hello"},
            ),
        ]
        for label, event_type, content, expected_alert in events:
            for room_id in ("!room:example.com", None):
                with self.subTest(case=label, room_id=room_id):
                    raw = _raw(
                        event_type=event_type,
                        room_name="Photos",
                        room_id=room_id,
                        content=content,
                        default_payload=default_payload,
                    )
                    # Stock trusts the caller's alert body, and its room_id whenever
                    # the homeserver sent none.
                    want = await self._send(raw, stock=True)
                    assert want is not None
                    self.assertEqual(want["aps"]["alert"]["body"], "Spoofed body")
                    self.assertEqual(want["room_id"], room_id or "!spoof:evil.example")

                    got = await self._send(raw)
                    assert got is not None
                    self.assertEqual(got["aps"]["alert"], expected_alert)
                    self.assertEqual(got.get("room_id"), room_id)
                    self.assertEqual(got["aps"].get("thread-id"), room_id)
                    self.assertNotIn("spoof", json.dumps(got).lower())

    async def test_3872_byte_default_payload_keeps_short_text_preview(self) -> None:
        default_payload = {"custom_data": ""}
        default_payload["custom_data"] = "x" * (3872 - _json_size(default_payload))
        self.assertEqual(_json_size(default_payload), 3872)
        raw = _raw(
            event_type="m.room.message",
            room_name="Engineering Room",
            content={"msgtype": "m.text", "body": "**hello**"},
            default_payload=default_payload,
        )
        want = await self._send(raw, stock=True)
        got = await self._send(raw)
        assert want is not None and got is not None
        self.assertEqual(
            want["aps"]["alert"]["loc-args"], ["Alice", "Engineering Room", "**hello**"]
        )
        # The gateway metadata does not fit next to the whole preview, so the push
        # degrades to the stock payload instead of truncating the preview to "".
        self.assertEqual(got, want)
        self.assertLessEqual(_json_size(got), 4096)

    async def test_forced_fallback_strips_spoofed_reserved_fields(self) -> None:
        # The sender ID is only in the enriched payload, and long enough here that the
        # enriched payload is larger than the stock one even without a thread-id.
        long_sender = "@" + "r" * 120 + ":example.com"
        for room_id in (REAL_IDENTITY["room_id"], None):
            with self.subTest(room_id=room_id):

                def _spoofed_raw(pad: int, room_id: Optional[str] = room_id) -> Dict[str, Any]:
                    return _raw(
                        event_type="m.room.message",
                        content={"msgtype": "m.text", "body": "**Hello**"},
                        default_payload={**SPOOFED_DEFAULT_PAYLOAD, "pad": "x" * pad},
                        **{**REAL_IDENTITY, "sender": long_sender, "room_id": room_id},
                    )

                def _expected(raw: Dict[str, Any], room_id: Optional[str] = room_id) -> Dict[str, Any]:
                    return _without_reserved_fields(
                        self._stock_payload(raw), homeserver_sent_room_id=room_id is not None
                    )

                # Pad until the stock payload minus the reserved fields exactly fills the
                # APNs limit, so not a single byte of gateway metadata can be added.
                raw = _spoofed_raw(4096 - _json_size(_expected(_spoofed_raw(0))))
                expected = _expected(raw)
                self.assertEqual(_json_size(expected), 4096)

                got = await self._send(raw)
                assert got is not None
                self.assertEqual(got, expected)
                self.assertEqual(
                    got["aps"]["alert"],
                    {
                        "action-loc-key": "OPEN",
                        "loc-key": "MSG_FROM_USER_IN_ROOM_WITH_CONTENT",
                        "loc-args": ["Real User", "Real Room", "**Hello**"],
                    },
                )
                self.assertEqual(got.get("room_id"), room_id)
                self.assertNotIn("spoof", json.dumps(got).lower())
                for key in ("sender", "sender_display_name", "room_name", RING_PAYLOAD_KEY, PLAIN_PAYLOAD_KEY):
                    self.assertNotIn(key, got)
                self.assertNotIn("thread-id", got["aps"])
                self.assertNotIn("mutable-content", got["aps"])
                # Stock cannot deliver this at all once the spoofed fields are counted.
                self.assertIsNone(await self._send(raw, stock=True))

    async def test_tight_budgets_keep_preview_and_never_restore_spoofed_fields(self) -> None:
        bodies = [
            ("short", "**hello**"),
            ("long_ascii", "**Deploy** " + "status ok " * 30),
            ("long_emoji", "🌍" * 300),
        ]
        default_payloads = [("spoofed", SPOOFED_DEFAULT_PAYLOAD), ("clean", {})]
        cases = [
            (payload_label, default_payload, body_label, body)
            for payload_label, default_payload in default_payloads
            for body_label, body in bodies
        ]
        stages_seen = set()
        for payload_label, default_payload, body_label, body in cases:
            clean = strip_markdown(body)
            for pad in range(3200, 4000, 16):
                with self.subTest(default_payload=payload_label, body=body_label, pad=pad):
                    raw = _raw(
                        event_type="m.room.message",
                        content={"msgtype": "m.text", "body": body},
                        default_payload={**default_payload, "pad": "x" * pad},
                        **REAL_IDENTITY,
                    )
                    want = await self._send(raw, stock=True)
                    got = await self._send(raw)
                    if want is not None:
                        # Whenever stock can deliver, so can we.
                        self.assertIsNotNone(got)
                    if got is None:
                        stages_seen.add((payload_label, body_label, "too_long"))
                        continue
                    self.assertLessEqual(_json_size(got), 4096)
                    self.assertNotIn("spoof", json.dumps(got).lower())
                    self.assertNotIn(RING_PAYLOAD_KEY, got)
                    alert = got["aps"]["alert"]
                    if "body" in alert:
                        stage = "rich" if "room_name" in got else "reduced"
                        preview = alert["body"]
                        self.assertEqual(got[PLAIN_PAYLOAD_KEY], 1)
                        self.assertEqual(got["sender"], "@real:example.com")
                        self.assertEqual(got["aps"]["thread-id"], "!real:example.com")
                        self.assertEqual("subtitle" in alert, stage == "rich")
                        self.assertEqual("sender_display_name" in got, stage == "rich")
                        self.assertTrue(preview)
                        self.assertTrue(clean.startswith(preview))
                        # Sygnal chops whole characters, so allow one partial 4-byte character.
                        self.assertGreaterEqual(
                            len(preview.encode("utf-8")),
                            min(len(clean.encode("utf-8")), MIN_PREVIEW_BYTES - 3),
                        )
                    else:
                        stage = "stock"
                        # Exactly what Sygnal makes of the stock payload minus reserved fields.
                        sanitized_stock = _without_reserved_fields(self._stock_payload(raw))
                        self.assertEqual(got, apnstruncate.truncate(sanitized_stock, 4096))
                        preview = alert["loc-args"][2]
                        if want is not None:
                            # Never less preview than stock would have delivered.
                            self.assertTrue(
                                preview.startswith(want["aps"]["alert"]["loc-args"][2])
                            )
                    stages_seen.add((payload_label, body_label, stage))
        self.assertEqual(
            stages_seen,
            {
                (payload_label, body_label, stage)
                for payload_label, _, body_label, _ in cases
                for stage in ("rich", "reduced", "stock", "too_long")
            },
        )

    def test_fits_with_preview_guarantees_sygnal_keeps_every_preview(self) -> None:
        rng = random.Random(20261122)
        alphabet = ["a", "é", "中", "🌍", '"', "\\", " "]

        def _text(max_chars: int) -> str:
            return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, max_chars)))

        accepted = rejected = 0
        for _ in range(400):
            alert: Dict[str, Any] = {"title": _text(40)}
            if rng.random() < 0.5:
                alert["body"] = _text(400)
            if "body" not in alert or rng.random() < 0.6:
                alert["loc-args"] = [
                    _text(rng.choice([5, 60, 400])) for _ in range(rng.randint(1, 3))
                ]
            payload = {"aps": {"alert": alert, "badge": 1}, "pad": "x" * rng.randint(3300, 4100)}
            if not self.pushkin._fits_with_preview(payload):
                rejected += 1
                continue
            accepted += 1
            # Sygnal's own truncation must succeed and leave every preview string
            # with at least its MIN_PREVIEW_BYTES prefix.
            shaved = apnstruncate.truncate(copy.deepcopy(payload), 4096)["aps"]["alert"]
            kept = [(alert[key], shaved[key]) for key in ("body",) if key in alert]
            kept.extend(zip(alert.get("loc-args", []), shaved.get("loc-args", [])))
            for original, remaining in kept:
                floor = original.encode("utf-8")[:MIN_PREVIEW_BYTES].decode("utf-8", "ignore")
                self.assertTrue(remaining.startswith(floor), (original, remaining))
        self.assertGreater(accepted, 50)
        self.assertGreater(rejected, 50)

    async def test_unshortenable_alert_near_limit_matches_stock(self) -> None:
        # Stock puts a non-string loc-arg in the alert for an image without a text body.
        # Sygnal cannot shorten such an alert, so metadata is only added if it fits whole.
        for pad, enriched in ((3000, True), (3900, False)):
            with self.subTest(pad=pad):
                raw = _raw(
                    event_type="m.room.message",
                    room_name="Photos",
                    content={"msgtype": "m.image", "body": None},
                    default_payload={"pad": "x" * pad},
                )
                want = await self._send(raw, stock=True)
                got = await self._send(raw)
                assert want is not None and got is not None
                self.assertEqual(got["aps"]["alert"], want["aps"]["alert"])
                self.assertEqual(got["aps"]["alert"]["loc-args"], ["Alice", None, "Photos"])
                if enriched:
                    self.assertEqual(got["sender"], "@alice:example.com")
                    self.assertEqual(got["aps"]["mutable-content"], 1)
                else:
                    self.assertEqual(got, want)

    async def test_sygnal_truncation_runs_exactly_once_per_dispatch(self) -> None:
        cases = [
            (
                "long_image_caption",
                _raw(
                    event_type="m.room.message",
                    room_name="Photos",
                    content={"msgtype": "m.image", "body": "caption " * 600},
                    default_payload={"pad": "x" * 3000},
                ),
            ),
            (
                "long_text",
                _raw(
                    event_type="m.room.message",
                    room_name="Engineering",
                    content={"msgtype": "m.text", "body": "status ok " * 600},
                    default_payload={"pad": "x" * 3000},
                ),
            ),
            (
                "long_text_forced_to_stock_fallback",
                _raw(
                    event_type="m.room.message",
                    room_name="Engineering",
                    content={"msgtype": "m.text", "body": "status ok " * 600},
                    default_payload={"pad": "x" * 3880},
                ),
            ),
        ]
        for label, raw in cases:
            with self.subTest(case=label):
                want = await self._send(raw, stock=True)
                with patch.object(
                    apnstruncate, "truncate", wraps=apnstruncate.truncate
                ) as truncate_spy:
                    msg = await self._send(raw)
                self.assertEqual(truncate_spy.call_count, 1)
                assert msg is not None and want is not None
                self.assertLessEqual(_json_size(msg), 4096)
                alert = msg["aps"]["alert"]
                if label.endswith("stock_fallback"):
                    self.assertEqual(msg, want)
                else:
                    preview = alert["body"] if "body" in alert else max(alert["loc-args"], key=len)
                    self.assertGreaterEqual(len(preview.encode("utf-8")), MIN_PREVIEW_BYTES)
                    self.assertEqual(msg["sender"], "@alice:example.com")

    async def test_lone_surrogates_never_drop_a_push(self) -> None:
        bad = "\ud83d"
        # (label, broken _raw kwargs, metadata key that must carry the replacement character)
        broken_fields: List[Tuple[str, Dict[str, Any], str]] = [
            ("sender", {"sender": f"@al{bad}ice:example.com"}, "sender"),
            (
                "sender_without_display_name",
                {"sender": f"@al{bad}ice:example.com", "sender_display_name": None},
                "sender",
            ),
            ("sender_display_name", {"sender_display_name": f"Al{bad}ice"}, "sender_display_name"),
            ("room_name", {"room_name": f"Eng{bad}ineering"}, "room_name"),
            ("room_alias", {"room_alias": f"#eng{bad}:example.com"}, "room_name"),
        ]
        events: List[Tuple[str, Dict[str, Any]]] = [
            ("text", {"event_type": "m.room.message", "content": {"msgtype": "m.text", "body": "**Hello**"}}),
            ("text_broken_body", {"event_type": "m.room.message", "content": {"msgtype": "m.text", "body": f"He{bad}llo"}}),
            ("emote", {"event_type": "m.room.message", "content": {"msgtype": "m.emote", "body": f"wa{bad}ves"}}),
            ("image", {"event_type": "m.room.message", "content": {"msgtype": "m.image", "body": f"sun{bad}set.jpg"}}),
            ("encrypted", {"event_type": ENCRYPTED_EVENT_TYPE, "content": {"algorithm": "m.megolm.v1.aes-sha2"}}),
            ("invite", {"event_type": "m.room.member", "user_is_target": True, "membership": "invite"}),
            ("legacy_call_invite", {"event_type": "m.call.invite", "content": {}}),
            ("ring", {"content": _ring(self.now_ms), "tweaks": {"sound": "ring"}}),
        ]
        for field_label, broken, metadata_key in broken_fields:
            for event_label, event in events:
                with self.subTest(field=field_label, event=event_label):
                    msg = await self._send(_raw(**{**event, **broken}))
                    assert msg is not None
                    # aioapns must be able to UTF-8 encode what we hand it.
                    encoded = json.dumps(msg, ensure_ascii=False).encode("utf-8")
                    self.assertLessEqual(len(encoded), 4096)
                    if event_label != "ring":
                        self.assertIn("\ufffd", msg[metadata_key])
        # Stock delivers a broken sender ID when a display name exists, so we must too.
        stock_msg = await self._send(
            _raw(
                event_type="m.room.message",
                sender=f"@al{bad}ice:example.com",
                content={"msgtype": "m.text", "body": "Hello"},
            ),
            stock=True,
        )
        assert stock_msg is not None
        json.dumps(stock_msg, ensure_ascii=False).encode("utf-8")

    async def test_expiry_and_retries(self) -> None:
        # 1. Expired at payload build time -> returns None (skips truncation, no BodyTooLong)
        oversized = {"aps": {"sound": "default"}, "pad": "x" * 5000}
        for age in (30_000, 60_000):
            with self.subTest(expired_age=age):
                self.sent[APP_ID].clear()
                notif = Notification(
                    _raw(
                        content=_ring(self.now_ms - age),
                        tweaks={"sound": "ring"},
                        default_payload=oversized,
                    )
                )
                log = NotificationLoggerAdapter(logging.getLogger(__name__), {"request_id": "req-exp"})
                self.assertIsNone(self.pushkin._get_payload_full(notif, notif.devices[0], log, True))
                self.assertEqual(await self._dispatch(notif), [])
                self.assertEqual(self.sent[APP_ID], [])

        # 2. Retry scenarios: (label, age_ms, succeed_on_attempt, advance_secs, expected_ttls)
        retry_cases = [
            ("expired_before_retry1", 25_000, None, [10], [5]),
            ("expired_before_retry2", 5_000, None, [10, 20], [25, 15]),
            ("live_retry_recomputes_ttl", 5_000, 2, [10], [25, 15]),
        ]
        for label, age, succeed_on, adv, expected_ttls in retry_cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                attempts = 0

                def _handler(req: NotificationRequest, ok_at: Optional[int] = succeed_on) -> NotificationResult:
                    nonlocal attempts
                    attempts += 1
                    if ok_at is not None and attempts >= ok_at:
                        return NotificationResult(req.notification_id, "200")
                    return NotificationResult(req.notification_id, "503", description="Busy")

                self.send_handler = _handler
                notif = Notification(_raw(content=_ring(self.now_ms - age), tweaks={"sound": "ring"}))
                self.assertEqual(await self._dispatch(notif, advance=adv), [])
                self.assertEqual([r.time_to_live for r in self.sent[APP_ID]], expected_ttls)

    async def test_safe_loader_fallback(self) -> None:
        loader_cfg = {
            **self.sygnal.config["apps"][APP_ID],
            "type": "mindroom_pushkin_loader.SafeApnsPushkin",
        }
        # 1. Valid pushkin -> returns MindroomApnsPushkin
        ok = await self._await(self.sygnal._make_pushkin(APP_ID, loader_cfg))
        self.assertIsInstance(ok, MindroomApnsPushkin)

        real_import = importlib.import_module

        def _fail_pushkin(exc: Exception) -> Callable[..., Any]:
            def _imp(name: str, *args: Any, **kwargs: Any) -> Any:
                if name == "mindroom_pushkin":
                    raise exc
                return real_import(name, *args, **kwargs)
            return _imp

        # 2. ImportError / SyntaxError on pushkin import -> falls back to stock ApnsPushkin
        for exc in (ImportError("broken"), SyntaxError("bad syntax")):
            with patch("importlib.import_module", side_effect=_fail_pushkin(exc)):
                fb = await self._await(self.sygnal._make_pushkin(APP_ID, loader_cfg))
            self.assertIs(type(fb), ApnsPushkin)

        # 3. Constructor raising -> logs error and falls back to stock ApnsPushkin
        with patch.object(MindroomApnsPushkin, "__init__", side_effect=RuntimeError("boom")):
            fallback_ctor = await self._await(self.sygnal._make_pushkin(APP_ID, loader_cfg))
        self.assertIs(type(fallback_ctor), ApnsPushkin)



FCM_APP_ID = "chat.mindroom.app.android"
FCM_PROJECT_ID = "mindroom-test-project"
FCM_SPOOFED_DEFAULT_PAYLOAD: Dict[str, Any] = {
    "sender": "@spoof:evil.example",
    "sender_display_name": "Spoofed Admin",
    "room_name": "Spoofed Room",
    RING_PAYLOAD_KEY: '{"expires_at":1,"call_type":"video","spoof":true}',
    PLAIN_PAYLOAD_KEY: "1",
    "room_id": "!spoof:evil.example",
    "event_id": "$spoof",
    "notification": {"title": "Spoofed", "body": "Spoofed"},
    "android": {"priority": "high", "notification": {"channel_id": "spoof"}},
    "from": "spoof",
    "message_type": "spoof",
    "google.c.a.e": "1",
    "gcm.notification.title": "spoof",
    "prio": "low",
    "unread": "99",
    "bad_int": 42,
    "bad_dict": {"nested": "x"},
    "bad_none": None,
    FCM_ACCOUNT_KEY: "@me:mindroom.chat",
    "custom": "kept",
}


class _FakeHttpResponse:
    def __init__(self, code: int = 200, body: bytes = b'{"name":"projects/p/messages/1"}', headers: Optional[Dict[bytes, List[bytes]]] = None) -> None:
        self.code, self.phrase, self.length, self._body = code, b"OK", len(body), body
        self.headers = Headers(headers or {})

    def deliverBody(self, protocol: Any) -> None:
        protocol.dataReceived(self._body)
        protocol.connectionLost(Failure(ResponseDone()))


class MindroomFcmPushkinTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now_ms = BASE_NOW_MS
        self.reactor = MemoryReactorClock()
        self.sent_requests: List[Tuple[bytes, bytes, Headers, bytes, Dict[str, Any]]] = []
        self.response_handler: Callable[[Dict[str, Any]], _FakeHttpResponse] = lambda _: _FakeHttpResponse()

        self.creds = MagicMock(valid=True, token="fake-bearer-token")

        async def _fake_request(method: bytes, url: bytes, headers: Headers, bodyProducer: Any = None) -> _FakeHttpResponse:
            raw_bytes = bodyProducer._inputFile.read() if bodyProducer is not None else b""
            parsed = json.loads(raw_bytes.decode("utf-8"))
            self.sent_requests.append((method, url, headers, raw_bytes, parsed))
            return self.response_handler(parsed)

        patch("sygnal.gcmpushkin.load_credentials_from_file", return_value=(self.creds, FCM_PROJECT_ID)).start()
        patch("sygnal.gcmpushkin.ProxyAgent.request", side_effect=_fake_request).start()
        patch.object(mindroom_pushkin, "now_ms", side_effect=lambda: self.now_ms).start()
        self.addCleanup(patch.stopall)

        self.fcm_app_cfg: Dict[str, Any] = {
            "type": "mindroom_pushkin.MindroomFcmPushkin",
            "api_version": "v1",
            "project_id": FCM_PROJECT_ID,
            "service_account_file": "/sygnal/fake-sa.json",
            "fcm_options": {"android": {"restricted_package_name": FCM_APP_ID}},
        }
        cfg = merge_left_with_defaults(
            CONFIG_DEFAULTS,
            {"log": {"setup": {"version": 1, "disable_existing_loggers": False}}, "apps": {FCM_APP_ID: self.fcm_app_cfg}},
        )
        self.sygnal = Sygnal(cfg, self.reactor)  # type: ignore[arg-type]
        self.pushkin = await self._await(self.sygnal._make_pushkin(FCM_APP_ID, cfg["apps"][FCM_APP_ID]))
        self.assertIsInstance(self.pushkin, MindroomFcmPushkin)

    async def _await(self, coro: Any, *, advance: Optional[List[int]] = None) -> Any:
        fut = ensureDeferred(coro).asFuture(asyncio.get_running_loop())
        for secs in advance or []:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if fut.done():
                break
            self.now_ms += secs * 1000
            self.reactor.advance(secs)
        return await fut

    def _fcm_raw(self, **kwargs: Any) -> Dict[str, Any]:
        raw = _raw(**kwargs)
        raw["devices"][0].update(app_id=FCM_APP_ID, pushkey="fcm-device-token-123")
        return raw

    async def _dispatch(self, raw: Dict[str, Any], *, pushkin: Optional[GcmPushkin] = None, advance: Optional[List[int]] = None) -> List[str]:
        notif = Notification(copy.deepcopy(raw))
        ctx = NotificationContext("req-fcm", self.sygnal.tracer.start_span("test"), 0.0)
        return await self._await((pushkin or self.pushkin).dispatch_notification(notif, notif.devices[0], ctx), advance=advance)

    async def _send_one(self, raw: Dict[str, Any], *, pushkin: Optional[GcmPushkin] = None) -> Dict[str, Any]:
        self.sent_requests.clear()
        self.assertEqual(await self._dispatch(raw, pushkin=pushkin), [])
        self.assertEqual(len(self.sent_requests), 1)
        method, url, headers, raw_bytes, parsed = self.sent_requests[0]
        self.assertEqual(method, b"POST")
        self.assertEqual(url, f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send".encode())
        self.assertEqual(headers.getRawHeaders(b"authorization"), [b"Bearer fake-bearer-token"])
        self.assertLessEqual(len(raw_bytes), MAX_FIREBASE_MESSAGE_SIZE)
        msg = parsed["message"]
        self.assertEqual(msg["token"], "fcm-device-token-123")
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in msg["data"].items()))
        return msg

    async def test_fcm_rich_alert_wire_payloads(self) -> None:
        event_id_only = {
            "room_id": "!room:example.com", "event_id": "$e1", "prio": "low", "counts": {"unread": 1},
            "devices": [{"app_id": FCM_APP_ID, "pushkey": "fcm-device-token-123"}],
        }
        cases: List[Tuple[str, Dict[str, Any], str, str, bool, str]] = [
            ("group_md", self._fcm_raw(event_type="m.room.message", room_name="Engineering", content={"msgtype": "m.text", "body": "> <@b:e> old\n\n🔧 `sh` [1]\n**Deploy** [docs](https://mindroom.chat)"}), "Engineering: Alice", "Deploy docs", True, "high"),
            ("dm", self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "*Hi* there"}), "Alice", "Hi there", True, "high"),
            ("dm_same_room", self._fcm_raw(event_type="m.room.message", room_name="Alice", content={"msgtype": "m.notice", "body": "**Ready**"}), "Alice", "Ready", True, "high"),
            ("emote", self._fcm_raw(event_type="m.room.message", room_name="Team", content={"msgtype": "m.emote", "body": "**waves**"}), "Team: Alice", "Alice waves", True, "high"),
            ("empty_strip", self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "---"}), "Alice", "New message", False, "high"),
            ("image", self._fcm_raw(event_type="m.room.message", room_name="Photos", content={"msgtype": "m.image", "body": "a.jpg"}), "Photos: Alice", "Sent an image", False, "high"),
            ("approval_named", self._fcm_raw(event_type=TOOL_APPROVAL_TYPE, room_name="Ops", sender_display_name="DevAgent", content={"status": "pending", "tool_name": "bash"}), "Ops: DevAgent", "Approval needed: bash", True, "high"),
            ("approval_unnamed", self._fcm_raw(event_type=TOOL_APPROVAL_TYPE, sender_display_name="DevAgent", content={"status": "pending"}), "DevAgent", "Approval needed", True, "high"),
            ("encrypted", self._fcm_raw(event_type=ENCRYPTED_EVENT_TYPE, room_name="Secret", content={"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"}), "Secret: Alice", "New message", False, "high"),
            ("event_id_only_low", event_id_only, "MindRoom", "New message", False, "normal"),
            ("invite", self._fcm_raw(event_type="m.room.member", room_name="Design", user_is_target=True, membership="invite"), "Design: Alice", "Invited you to Design", False, "high"),
            ("legacy_video_call", self._fcm_raw(event_type="m.call.invite", content={"offer": {"sdp": "v=0\r\nm=video 9\r\n"}}), "Alice", "Incoming video call", False, "high"),
        ]
        for label, raw, want_title, want_body, want_plain, want_prio in cases:
            with self.subTest(case=label):
                msg = await self._send_one(raw)
                self.assertEqual(msg["notification"], {"title": want_title, "body": want_body})
                self.assertEqual(msg["android"]["priority"], want_prio)
                self.assertEqual(msg["android"]["restricted_package_name"], FCM_APP_ID)
                self.assertEqual(msg["android"]["notification"], {"channel_id": FCM_MESSAGE_CHANNEL_ID, "sound": "default", "tag": "!room:example.com"})
                self.assertEqual(msg["data"]["room_id"], "!room:example.com")
                self.assertEqual(msg["data"].get(PLAIN_PAYLOAD_KEY), "1" if want_plain else None)
                self.assertNotIn(RING_PAYLOAD_KEY, msg["data"])

        # Long room_id tag is UTF-8 bounded; missing room_id omits tag; fcm_options never mutated.
        msg_long = await self._send_one(self._fcm_raw(event_type="m.room.message", room_id="!" + ("é" * 200) + ":ex", content={"msgtype": "m.text", "body": "hi"}))
        self.assertLessEqual(len(msg_long["android"]["notification"]["tag"].encode("utf-8")), MAX_THREAD_ID_BYTES)
        msg_no_room = await self._send_one(self._fcm_raw(event_type="m.room.message", room_id=None, content={"msgtype": "m.text", "body": "hi"}))
        self.assertNotIn("tag", msg_no_room["android"]["notification"])
        self.assertNotIn("room_id", msg_no_room["data"])
        self.assertEqual(self.pushkin.base_request_body, {"android": {"restricted_package_name": FCM_APP_ID}})

    async def test_fcm_drops_badge_only_and_non_alert_events(self) -> None:
        dev_spoof = [{"app_id": FCM_APP_ID, "pushkey": "fcm-device-token-123", "data": {"default_payload": FCM_SPOOFED_DEFAULT_PAYLOAD}}]
        for label, raw in (
            ("redaction", self._fcm_raw(event_type=REDACTION_EVENT_TYPE)),
            ("internal_io", self._fcm_raw(event_type="io.mindroom.internal")),
            ("internal_com", self._fcm_raw(event_type="com.mindroom.sync")),
            ("tool_approved", self._fcm_raw(event_type=TOOL_APPROVAL_TYPE, content={"status": "approved", "tool_name": "bash"})),
            ("tool_missing_status", self._fcm_raw(event_type=TOOL_APPROVAL_TYPE, content={"tool_name": "bash"})),
            ("badge_zero", {"counts": {"unread": 0}, "devices": dev_spoof}),
        ):
            with self.subTest(dropped=label):
                self.sent_requests.clear()
                self.assertEqual(await self._dispatch(raw), [])
                self.assertEqual(self.sent_requests, [])

        # Badge-only with counts sends data-only (no notification banner); disabled send_badge_counts drops it.
        badge_raw = {"counts": {"unread": 4}, "prio": "low", "devices": dev_spoof}
        badge_msg = await self._send_one(badge_raw)
        self.assertNotIn("notification", badge_msg)
        self.assertNotIn("notification", badge_msg["android"])
        self.assertEqual(badge_msg["android"]["priority"], "normal")
        self.assertEqual(badge_msg["data"]["unread"], "4")
        self.assertEqual(badge_msg["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")
        self.assertNotIn("room_id", badge_msg["data"])
        self.assertNotIn("event_id", badge_msg["data"])

        no_badge = await self._await(self.sygnal._make_pushkin(FCM_APP_ID, {**self.fcm_app_cfg, "send_badge_counts": False}))
        self.sent_requests.clear()
        self.assertEqual(await self._dispatch(badge_raw, pushkin=no_badge), [])
        self.assertEqual(self.sent_requests, [])

        # Non-invite member event stays data-only; non-dict default_payload rejects pushkey.
        join_msg = await self._send_one(self._fcm_raw(event_type="m.room.member", membership="join", user_is_target=False))
        self.assertNotIn("notification", join_msg)

        self.sent_requests.clear()
        bad_raw = self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "hi"})
        bad_raw["devices"][0]["data"] = {"default_payload": "not-a-dict"}
        self.assertEqual(await self._dispatch(bad_raw), ["fcm-device-token-123"])
        self.assertEqual(self.sent_requests, [])

    async def test_fcm_call_rings_expiry_and_retries(self) -> None:
        voice = await self._send_one(self._fcm_raw(content=_ring(self.now_ms, 30_000, "audio"), prio="low", tweaks={"sound": "ring"}))
        self.assertEqual(voice["notification"], {"title": "Alice", "body": "Incoming voice call"})
        self.assertEqual(voice["android"], {"priority": "high", "ttl": "30s", "restricted_package_name": FCM_APP_ID, "notification": {"channel_id": FCM_CALL_CHANNEL_ID, "sound": "default", "tag": "!room:example.com"}})
        self.assertEqual(json.loads(voice["data"][RING_PAYLOAD_KEY]), {"expires_at": self.now_ms + 30_000, "call_type": "voice"})

        video = await self._send_one(
            self._fcm_raw(event_type=ENCRYPTED_EVENT_TYPE, sender_display_name=None, room_name="Standup", room_id=None, content=_enc(_ring(self.now_ms, 600_000, "video")), tweaks={"sound": "ring"})
        )
        self.assertEqual(video["notification"], {"title": "Standup", "body": "Incoming video call"})
        self.assertEqual(video["android"]["ttl"], "120s")
        self.assertNotIn("tag", video["android"]["notification"])
        self.assertEqual(json.loads(video["data"][RING_PAYLOAD_KEY]), {"expires_at": self.now_ms + MAX_RING_LIFETIME_MS, "call_type": "video"})

        anon = await self._send_one(self._fcm_raw(sender=None, sender_display_name=None, room_name=None, content=_ring(self.now_ms), tweaks={"sound": "ring"}))
        self.assertEqual(anon["notification"]["title"], "MindRoom")

        for label, raw in (
            ("no_ring_tweak", self._fcm_raw(content=_ring(self.now_ms))),
            ("future_skew", self._fcm_raw(content=_ring(self.now_ms + MAX_CLOCK_SKEW_MS + 1), tweaks={"sound": "ring"})),
        ):
            with self.subTest(non_ring=label):
                msg = await self._send_one(raw)
                self.assertEqual(msg["android"]["notification"]["channel_id"], FCM_MESSAGE_CHANNEL_ID)
                self.assertNotIn(RING_PAYLOAD_KEY, msg["data"])

        # Expired at build time -> zero HTTP calls.
        self.sent_requests.clear()
        self.assertEqual(await self._dispatch(self._fcm_raw(content=_ring(self.now_ms - 30_000), tweaks={"sound": "ring"})), [])
        self.assertEqual(self.sent_requests, [])

        # Retry scenarios (503 & 429): recomputes TTL on live retry, drops expired rings before retries.
        for label, age_ms, status, resp_hdrs, ok_on, adv, want_ttls in (
            ("503_expired_before_retry1", 25_000, 503, None, None, [10], ["5s"]),
            ("503_expired_before_retry2", 5_000, 503, None, None, [10, 20], ["25s", "15s"]),
            ("503_live_retry", 5_000, 503, None, 2, [10], ["25s", "15s"]),
            ("429_default_60s_drops_ring", 5_000, 429, None, None, [60], ["25s"]),
            ("429_short_retry_after", 5_000, 429, {b"retry-after": [b"5"]}, 2, [5], ["25s", "20s"]),
        ):
            with self.subTest(retry=label):
                self.sent_requests.clear()
                attempts = 0

                def _handler(_: Dict[str, Any], st: int = status, hdrs: Any = resp_hdrs, ok: Optional[int] = ok_on) -> _FakeHttpResponse:
                    nonlocal attempts
                    attempts += 1
                    return _FakeHttpResponse(200) if (ok is not None and attempts >= ok) else _FakeHttpResponse(st, b"busy", hdrs)

                self.response_handler = _handler
                raw = self._fcm_raw(content=_ring(self.now_ms - age_ms), tweaks={"sound": "ring"})
                self.assertEqual(await self._dispatch(raw, advance=adv), [])
                self.assertEqual([req[4]["message"]["android"]["ttl"] for req in self.sent_requests], want_ttls)

        # HTTP 404 rejects unregistered pushkey upstream.
        self.response_handler = lambda _: _FakeHttpResponse(404, b"unregistered")
        self.assertEqual(await self._dispatch(self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "hi"})), ["fcm-device-token-123"])

    async def test_fcm_hygiene_spoofing_surrogates_and_size_limit(self) -> None:
        msg = await self._send_one(
            self._fcm_raw(event_type=ENCRYPTED_EVENT_TYPE, room_id=None, content={"algorithm": "m.megolm.v1.aes-sha2"}, default_payload=FCM_SPOOFED_DEFAULT_PAYLOAD)
        )
        for key in ("sender", "sender_display_name", "room_name", RING_PAYLOAD_KEY, PLAIN_PAYLOAD_KEY, "room_id", "notification", "android", "from", "message_type", "google.c.a.e", "gcm.notification.title", "bad_int", "bad_dict", "bad_none"):
            self.assertNotIn(key, msg["data"])
        self.assertEqual(msg["data"]["event_id"], "$event1")
        self.assertEqual(msg["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")
        self.assertEqual(msg["data"]["custom"], "kept")

        for bad_acct in ("", "   ", 123, None, "a\nb", "a\ud83db", "a" * (MAX_ACCOUNT_BYTES + 1)):
            with self.subTest(bad_account=bad_acct):
                m = await self._send_one(self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "hi"}, default_payload={FCM_ACCOUNT_KEY: bad_acct}))
                self.assertNotIn(FCM_ACCOUNT_KEY, m["data"])

        bad = "\ud83d"
        surrogate_msg = await self._send_one(
            self._fcm_raw(event_type="m.room.message", sender=f"@al{bad}ice:ex", sender_display_name=f"Al{bad}ice", room_name=f"Eng{bad}", room_id=f"!r{bad}:ex", content={"msgtype": "m.text", "body": f"He{bad}llo"}, default_payload={"custom": f"val{bad}"})
        )
        json.dumps(surrogate_msg, ensure_ascii=False).encode("utf-8")
        self.assertIn("\ufffd", surrogate_msg["notification"]["body"])

        padded = await self._send_one(self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "Short preview"}, default_payload={"pad": "x" * 5000, FCM_ACCOUNT_KEY: "@me:mindroom.chat"}))
        self.assertNotIn("pad", padded["data"])
        self.assertEqual(padded["notification"]["body"], "Short preview")
        self.assertEqual(padded["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")

        huge_emoji = await self._send_one(self._fcm_raw(event_type="m.room.message", content={"msgtype": "m.text", "body": "🔥" * 500}, default_payload={FCM_ACCOUNT_KEY: "@me:mindroom.chat"}))
        self.assertTrue(huge_emoji["notification"]["body"].endswith("…"))
        self.assertGreaterEqual(len(huge_emoji["notification"]["body"].encode("utf-8")), MIN_PREVIEW_BYTES)
        self.assertEqual(huge_emoji["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")

        # Extreme overhead (large fcm_options + long room/event IDs) still fits <= 4096 by shedding optional metadata.
        tight = await self._await(self.sygnal._make_pushkin(FCM_APP_ID, {**self.fcm_app_cfg, "fcm_options": {"android": {"collapse_key": "k" * 2400}}}))
        tight_msg = await self._send_one(
            self._fcm_raw(event_type="m.room.message", room_id="!" + "r" * 600, content={"msgtype": "m.text", "body": "Hello " * 200}),
            pushkin=tight,
        )
        self.assertTrue(tight_msg["notification"]["body"].endswith("…"))

    async def test_safe_fcm_loader_and_legacy_version(self) -> None:
        loader_cfg = {**self.fcm_app_cfg, "type": "mindroom_pushkin_loader.SafeFcmPushkin"}
        self.assertIsInstance(await self._await(self.sygnal._make_pushkin(FCM_APP_ID, loader_cfg)), MindroomFcmPushkin)

        for legacy_cfg in (
            {"api_version": "legacy", "api_key": "k"},
            {"api_key": "k"},
        ):
            with self.assertRaises(PushkinSetupException):
                await self._await(self.sygnal._make_pushkin(FCM_APP_ID, {**legacy_cfg, "type": "mindroom_pushkin.MindroomFcmPushkin"}))
            fb_legacy = await self._await(self.sygnal._make_pushkin(FCM_APP_ID, {**legacy_cfg, "type": "mindroom_pushkin_loader.SafeFcmPushkin"}))
            self.assertIs(type(fb_legacy), GcmPushkin)

        real_import = importlib.import_module
        for exc in (ImportError("broken"), SyntaxError("bad")):
            with patch("importlib.import_module", side_effect=lambda name, *a, e=exc, **kw: (_ for _ in ()).throw(e) if name == "mindroom_pushkin" else real_import(name, *a, **kw)):
                self.assertIs(type(await self._await(self.sygnal._make_pushkin(FCM_APP_ID, loader_cfg))), GcmPushkin)

        with patch.object(MindroomFcmPushkin, "__init__", side_effect=RuntimeError("boom")):
            self.assertIs(type(await self._await(self.sygnal._make_pushkin(FCM_APP_ID, loader_cfg))), GcmPushkin)

if __name__ == "__main__":
    unittest.main()
