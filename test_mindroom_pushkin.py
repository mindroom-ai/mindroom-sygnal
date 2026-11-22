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
    RING_HINT_KEY,
    RING_PAYLOAD_KEY,
    RING_SOUND,
    RTC_NOTIFICATION_TYPE,
    MindroomApnsPushkin,
    _truncate_utf8,
    strip_markdown,
)
from sygnal.apnspushkin import ApnsPushkin
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
        ]
        for label, raw, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(strip_markdown(raw), expected)

    def test_pathological_inputs_are_fast_and_bounded(self) -> None:
        patterns = [
            ("unclosed_links", "[a](" * 50_000),
            ("long_unclosed_paren", "[label](" + "a" * 100_000),
            ("nested_parens", "[label](" + "(a)" * 50_000),
            ("unclosed_bold", "**a " * 50_000),
            ("unclosed_italic", "*a " * 50_000),
            ("unclosed_strike", "~~a " * 50_000),
            ("long_hr_miss", "- " * 50_000 + "x"),
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
                self.assertEqual(got.message.get(RING_PAYLOAD_KEY), want.message.get(RING_PAYLOAD_KEY))
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
        ]
        for label, raw, expected_alert in cases:
            with self.subTest(case=label):
                self.sent[APP_ID].clear()
                self.assertEqual(await self._dispatch(Notification(raw)), [])
                msg = self.sent[APP_ID][0].message
                self.assertEqual(msg["aps"]["alert"], expected_alert)
                self.assertEqual(msg["aps"]["mutable-content"], 1)
                self.assertEqual(msg["aps"]["thread-id"], "!room:example.com")
                self.assertEqual(msg["sender"], "@alice:example.com")

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


if __name__ == "__main__":
    unittest.main()
