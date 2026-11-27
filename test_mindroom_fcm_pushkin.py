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
"""Unit tests for MindroomFcmPushkin and SafeFcmPushkin inside Sygnal v0.17.0."""

import asyncio
import copy
import importlib
import json
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from twisted.internet.defer import ensureDeferred
from twisted.internet.testing import MemoryReactorClock
from twisted.python.failure import Failure
from twisted.web.client import ResponseDone
from twisted.web.http_headers import Headers

import mindroom_fcm_pushkin
import mindroom_pushkin
from mindroom_fcm_pushkin import (
    FCM_ACCOUNT_KEY,
    FCM_CALL_CHANNEL_ID,
    FCM_MESSAGE_CHANNEL_ID,
    FCM_SILENT_MESSAGE_CHANNEL_ID,
    MAX_ACCOUNT_BYTES,
    MindroomFcmPushkin,
)
from mindroom_pushkin import (
    ENCRYPTED_EVENT_TYPE,
    MAX_CLOCK_SKEW_MS,
    MAX_RING_LIFETIME_MS,
    MAX_THREAD_ID_BYTES,
    MIN_PREVIEW_BYTES,
    PLAIN_PAYLOAD_KEY,
    REDACTION_EVENT_TYPE,
    RING_HINT_KEY,
    RING_PAYLOAD_KEY,
    RTC_NOTIFICATION_TYPE,
    TOOL_APPROVAL_TYPE,
)
from sygnal.exceptions import PushkinSetupException
from sygnal.gcmpushkin import MAX_FIREBASE_MESSAGE_SIZE, GcmPushkin
from sygnal.notifications import Notification, NotificationContext
from sygnal.sygnal import CONFIG_DEFAULTS, Sygnal, merge_left_with_defaults

FCM_APP_ID = "com.mindroom_ai.app"
FCM_PROJECT_ID = "mindroom-test-project"
BASE_NOW_MS = 1_795_300_000_000

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


def _ring(sender_ts: int, lifetime: int = 30_000, intent: Optional[str] = "audio") -> Dict[str, Any]:
    fields: Dict[str, Any] = {"notification_type": "ring", "sender_ts": sender_ts, "lifetime": lifetime}
    if intent is not None:
        fields["m.call.intent"] = intent
    return fields


def _enc(hint: Any) -> Dict[str, Any]:
    return {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque", RING_HINT_KEY: hint}


_DEFAULT_TWEAKS: Any = object()


def _raw(
    *,
    event_type: Optional[str] = RTC_NOTIFICATION_TYPE,
    content: Optional[Dict[str, Any]] = None,
    sender: Optional[str] = "@alice:example.com",
    sender_display_name: Optional[str] = "Alice",
    room_name: Optional[str] = None,
    room_id: Optional[str] = "!room:example.com",
    user_is_target: Optional[bool] = None,
    membership: Optional[str] = None,
    prio: str = "high",
    tweaks: Any = _DEFAULT_TWEAKS,
    default_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    dev: Dict[str, Any] = {"app_id": FCM_APP_ID, "pushkey": "fcm-device-token-123"}
    if tweaks is _DEFAULT_TWEAKS:
        dev["tweaks"] = {"sound": "default"}
    elif tweaks is not None:
        dev["tweaks"] = tweaks
    if default_payload is not None:
        dev["data"] = {"default_payload": default_payload}
    raw: Dict[str, Any] = {"event_id": "$event1", "prio": prio, "counts": {"unread": 1}, "devices": [dev]}
    for key, val in (
        ("room_id", room_id), ("type", event_type), ("content", content), ("sender", sender),
        ("sender_display_name", sender_display_name), ("room_name", room_name),
        ("user_is_target", user_is_target), ("membership", membership),
    ):
        if val is not None:
            raw[key] = val
    return raw


def _msg(body: str, msgtype: str = "m.text", **kwargs: Any) -> Dict[str, Any]:
    return _raw(event_type="m.room.message", content={"msgtype": msgtype, "body": body}, **kwargs)


class _FakeHttpResponse:
    def __init__(
        self,
        code: int = 200,
        body: bytes = b'{"name":"projects/p/messages/1"}',
        headers: Optional[Dict[bytes, List[bytes]]] = None,
    ) -> None:
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

        async def _fake_request(
            method: bytes, url: bytes, headers: Headers, bodyProducer: Any = None
        ) -> _FakeHttpResponse:
            raw_bytes = bodyProducer._inputFile.read() if bodyProducer is not None else b""
            parsed = json.loads(raw_bytes.decode("utf-8"))
            self.sent_requests.append((method, url, headers, raw_bytes, parsed))
            return self.response_handler(parsed)

        patch("sygnal.gcmpushkin.load_credentials_from_file", return_value=(self.creds, FCM_PROJECT_ID)).start()
        patch("sygnal.gcmpushkin.ProxyAgent.request", side_effect=_fake_request).start()
        patch.object(mindroom_pushkin, "now_ms", side_effect=lambda: self.now_ms).start()
        self.addCleanup(patch.stopall)

        self.fcm_app_cfg: Dict[str, Any] = {
            "type": "mindroom_fcm_pushkin.MindroomFcmPushkin",
            "api_version": "v1",
            "project_id": FCM_PROJECT_ID,
            "service_account_file": "/sygnal/fake-sa.json",
            "fcm_options": {"android": {"restricted_package_name": FCM_APP_ID}},
        }
        cfg = merge_left_with_defaults(
            CONFIG_DEFAULTS,
            {
                "log": {"setup": {"version": 1, "disable_existing_loggers": False}},
                "apps": {FCM_APP_ID: self.fcm_app_cfg},
            },
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

    async def _dispatch(
        self, raw: Dict[str, Any], *, pushkin: Optional[GcmPushkin] = None, advance: Optional[List[int]] = None
    ) -> List[str]:
        notif = Notification(copy.deepcopy(raw))
        ctx = NotificationContext("req-fcm", self.sygnal.tracer.start_span("test"), 0.0)
        return await self._await(
            (pushkin or self.pushkin).dispatch_notification(notif, notif.devices[0], ctx), advance=advance
        )

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
        self.assertNotIn("data", msg["android"])
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in msg["data"].items()))
        return msg

    async def test_fcm_rich_alert_wire_payloads(self) -> None:
        event_id_only = {
            "room_id": "!room:example.com", "event_id": "$e1", "prio": "low", "counts": {"unread": 1},
            "devices": [{"app_id": FCM_APP_ID, "pushkey": "fcm-device-token-123", "tweaks": {"sound": "default"}}],
        }
        md = "> <@b:e> old\n\n🔧 `sh` [1]\n**Deploy** [docs](https://mindroom.chat)"
        cases: List[Tuple[str, Dict[str, Any], str, str, bool, str]] = [
            ("group_md", _msg(md, room_name="Engineering"), "Engineering: Alice", "Deploy docs", True, "high"),
            ("dm", _msg("*Hi*!"), "Alice", "Hi!", True, "high"),
            ("dm_same_room", _msg("**Ready**", "m.notice", room_name="Alice"), "Alice", "Ready", True, "high"),
            ("emote", _msg("**waves**", "m.emote", room_name="Team"), "Team: Alice", "Alice waves", True, "high"),
            ("empty_strip", _msg("---"), "Alice", "New message", False, "high"),
            ("image", _msg("a.jpg", "m.image", room_name="Photos"), "Photos: Alice", "Sent an image", False, "high"),
            (
                "approval_named",
                _raw(
                    event_type=TOOL_APPROVAL_TYPE, room_name="Ops", sender_display_name="DevAgent",
                    content={"status": "pending", "tool_name": "bash"},
                ),
                "Ops: DevAgent", "Approval needed: bash", True, "high",
            ),
            (
                "approval_unnamed",
                _raw(event_type=TOOL_APPROVAL_TYPE, sender_display_name="DevAgent", content={"status": "pending"}),
                "DevAgent", "Approval needed", True, "high",
            ),
            (
                "encrypted",
                _raw(
                    event_type=ENCRYPTED_EVENT_TYPE, room_name="Secret",
                    content={"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "opaque"},
                ),
                "Secret: Alice", "New message", False, "high",
            ),
            ("event_id_only_low", event_id_only, "MindRoom", "New message", False, "normal"),
            (
                "invite",
                _raw(event_type="m.room.member", room_name="Design", user_is_target=True, membership="invite"),
                "Design: Alice", "Invited you to Design", False, "high",
            ),
            (
                "legacy_video_call",
                _raw(event_type="m.call.invite", content={"offer": {"sdp": "v=0\r\nm=video 9\r\n"}}),
                "Alice", "Incoming video call", False, "high",
            ),
        ]
        for label, raw, want_title, want_body, want_plain, want_prio in cases:
            with self.subTest(case=label):
                msg = await self._send_one(raw)
                self.assertEqual(msg["notification"], {"title": want_title, "body": want_body})
                self.assertEqual(msg["android"]["priority"], want_prio)
                self.assertEqual(msg["android"]["restricted_package_name"], FCM_APP_ID)
                self.assertEqual(
                    msg["android"]["notification"],
                    {"channel_id": FCM_MESSAGE_CHANNEL_ID, "sound": "default", "tag": "!room:example.com"},
                )
                self.assertEqual(msg["data"]["room_id"], "!room:example.com")
                self.assertEqual(msg["data"].get(PLAIN_PAYLOAD_KEY), "1" if want_plain else None)
                self.assertNotIn(RING_PAYLOAD_KEY, msg["data"])

        msg_long = await self._send_one(_msg("hi", room_id="!" + ("é" * 200) + ":ex"))
        self.assertLessEqual(len(msg_long["android"]["notification"]["tag"].encode("utf-8")), MAX_THREAD_ID_BYTES)
        msg_no_room = await self._send_one(_msg("hi", room_id=None))
        self.assertNotIn("tag", msg_no_room["android"]["notification"])
        self.assertNotIn("room_id", msg_no_room["data"])

        # Silent push rules (no sound tweak / empty / whitespace) use silent channel with no sound.
        for label, silent_tweaks in (
            ("omitted", None), ("empty_dict", {}), ("empty_str", {"sound": ""}), ("spaces", {"sound": "   "})
        ):
            with self.subTest(silent=label):
                silent = await self._send_one(_msg("Quiet update", tweaks=silent_tweaks))
                self.assertEqual(
                    silent["android"]["notification"],
                    {"channel_id": FCM_SILENT_MESSAGE_CHANNEL_ID, "tag": "!room:example.com"},
                )

    async def test_fcm_options_precedence_and_badge_banner_stripping(self) -> None:
        rich_opts = {
            "notification": {"title": "Config Banner", "body": "Should not leak"},
            "data": {"top_opt": "top_val", "room_id": "!from-top:ex"},
            "android": {
                "restricted_package_name": FCM_APP_ID,
                "notification": {
                    "icon": "ic_stat",
                    "color": "#112233",
                    "sound": "custom.ogg",
                    "tag": "stale-tag",
                    "title": "Override Title",
                    "body": "Override Body",
                    "title_loc_key": "T_KEY",
                    "title_loc_args": ["x"],
                    "body_loc_key": "B_KEY",
                    "body_loc_args": ["y"],
                },
                "data": {
                    "room_id": "!from-android:ex",
                    "event_id": "$from-android",
                    FCM_ACCOUNT_KEY: "@from-android:ex",
                    RING_PAYLOAD_KEY: "from-android",
                    PLAIN_PAYLOAD_KEY: "1",
                    "env": "prod",
                    "bad_int": 9,
                },
            },
        }
        orig_opts = copy.deepcopy(rich_opts)
        pushkin = await self._await(
            self.sygnal._make_pushkin(FCM_APP_ID, {**self.fcm_app_cfg, "fcm_options": rich_opts})
        )

        # Alert push: android.data removed, safe config strings merged under gateway fields.
        alert = await self._send_one(
            _msg("**Hello**", default_payload={FCM_ACCOUNT_KEY: "@me:mindroom.chat"}), pushkin=pushkin
        )
        self.assertEqual(alert["notification"], {"title": "Alice", "body": "Hello"})
        self.assertEqual(
            alert["android"]["notification"],
            {
                "icon": "ic_stat",
                "color": "#112233",
                "channel_id": FCM_MESSAGE_CHANNEL_ID,
                "sound": "default",
                "tag": "!room:example.com",
            },
        )

        # Silent alert strips fcm_options sound, default_sound, defaultSound, and title/body overrides.
        silent_opts = copy.deepcopy(rich_opts)
        silent_opts["android"]["notification"].update(default_sound=True, defaultSound=True)
        silent_pushkin = await self._await(
            self.sygnal._make_pushkin(FCM_APP_ID, {**self.fcm_app_cfg, "fcm_options": silent_opts})
        )
        silent_alert = await self._send_one(_msg("Quiet", tweaks=None), pushkin=silent_pushkin)
        self.assertEqual(
            silent_alert["android"]["notification"],
            {
                "icon": "ic_stat",
                "color": "#112233",
                "channel_id": FCM_SILENT_MESSAGE_CHANNEL_ID,
                "tag": "!room:example.com",
            },
        )
        self.assertNotIn("default_sound", silent_alert["android"]["notification"])
        self.assertNotIn("defaultSound", silent_alert["android"]["notification"])
        self.assertEqual(alert["data"]["room_id"], "!room:example.com")
        self.assertEqual(alert["data"]["event_id"], "$event1")
        self.assertEqual(alert["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")
        self.assertEqual(alert["data"]["env"], "prod")
        self.assertEqual(alert["data"]["top_opt"], "top_val")
        self.assertNotIn("bad_int", alert["data"])
        self.assertNotIn(RING_PAYLOAD_KEY, alert["data"])

        # Ring push: gateway ring payload and call channel win over android.data / config banner.
        ring = await self._send_one(
            _raw(content=_ring(self.now_ms, 30_000, "video"), tweaks={"sound": "ring"}), pushkin=pushkin
        )
        self.assertEqual(
            ring["android"]["notification"],
            {
                "icon": "ic_stat",
                "color": "#112233",
                "channel_id": FCM_CALL_CHANNEL_ID,
                "sound": "default",
                "tag": "call:!room:example.com",
            },
        )
        self.assertEqual(
            json.loads(ring["data"][RING_PAYLOAD_KEY]),
            {"expires_at": self.now_ms + 30_000, "call_type": "video"},
        )
        self.assertEqual(ring["data"]["env"], "prod")

        # Roomless alert clears stale config tag and does not leak config room_id.
        no_room = await self._send_one(_msg("hi", room_id=None), pushkin=pushkin)
        self.assertNotIn("tag", no_room["android"]["notification"])
        self.assertNotIn("room_id", no_room["data"])

        # Badge-only & non-invite member pushes strip BOTH message.notification & android.notification.
        badge = await self._send_one(
            {"counts": {"unread": 3}, "devices": [{"app_id": FCM_APP_ID, "pushkey": "fcm-device-token-123"}]},
            pushkin=pushkin,
        )
        self.assertNotIn("notification", badge)
        self.assertNotIn("notification", badge["android"])
        self.assertEqual(badge["data"]["unread"], "3")
        self.assertEqual(badge["data"]["env"], "prod")
        self.assertNotIn("room_id", badge["data"])
        self.assertNotIn(RING_PAYLOAD_KEY, badge["data"])

        join = await self._send_one(
            _raw(event_type="m.room.member", membership="join", user_is_target=False), pushkin=pushkin
        )
        self.assertNotIn("notification", join)
        self.assertNotIn("notification", join["android"])
        self.assertEqual(pushkin.base_request_body, orig_opts)

    async def test_fcm_drops_and_badge_only(self) -> None:
        dev_spoof = [
            {
                "app_id": FCM_APP_ID,
                "pushkey": "fcm-device-token-123",
                "data": {"default_payload": FCM_SPOOFED_DEFAULT_PAYLOAD},
            }
        ]
        for label, raw in (
            ("redaction", _raw(event_type=REDACTION_EVENT_TYPE)),
            ("internal_io", _raw(event_type="io.mindroom.internal")),
            ("internal_com", _raw(event_type="com.mindroom.sync")),
            ("tool_approved", _raw(event_type=TOOL_APPROVAL_TYPE, content={"status": "approved", "tool_name": "bash"})),
            ("tool_missing_status", _raw(event_type=TOOL_APPROVAL_TYPE, content={"tool_name": "bash"})),
            ("badge_zero", {"counts": {"unread": 0}, "devices": dev_spoof}),
        ):
            with self.subTest(dropped=label):
                self.sent_requests.clear()
                self.assertEqual(await self._dispatch(raw), [])
                self.assertEqual(self.sent_requests, [])

        badge_raw = {"counts": {"unread": 4}, "prio": "low", "devices": dev_spoof}
        badge_msg = await self._send_one(badge_raw)
        self.assertNotIn("notification", badge_msg)
        self.assertNotIn("notification", badge_msg["android"])
        self.assertEqual(badge_msg["android"]["priority"], "normal")
        self.assertEqual(badge_msg["data"]["unread"], "4")
        self.assertEqual(badge_msg["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")
        self.assertNotIn("room_id", badge_msg["data"])

        no_badge = await self._await(
            self.sygnal._make_pushkin(FCM_APP_ID, {**self.fcm_app_cfg, "send_badge_counts": False})
        )
        self.sent_requests.clear()
        self.assertEqual(await self._dispatch(badge_raw, pushkin=no_badge), [])
        self.assertEqual(self.sent_requests, [])

        self.sent_requests.clear()
        bad_raw = _msg("hi")
        bad_raw["devices"][0]["data"] = {"default_payload": "not-a-dict"}
        self.assertEqual(await self._dispatch(bad_raw), ["fcm-device-token-123"])
        self.assertEqual(self.sent_requests, [])

    async def test_fcm_call_rings_expiry_and_retries(self) -> None:
        voice = await self._send_one(
            _raw(content=_ring(self.now_ms, 30_000, "audio"), prio="low", tweaks={"sound": "ring"})
        )
        self.assertEqual(voice["notification"], {"title": "Alice", "body": "Incoming voice call"})
        self.assertEqual(
            voice["android"],
            {
                "priority": "high",
                "ttl": "30s",
                "restricted_package_name": FCM_APP_ID,
                "notification": {
                    "channel_id": FCM_CALL_CHANNEL_ID,
                    "sound": "default",
                    "tag": "call:!room:example.com",
                },
            },
        )
        self.assertEqual(
            json.loads(voice["data"][RING_PAYLOAD_KEY]),
            {"expires_at": self.now_ms + 30_000, "call_type": "voice"},
        )

        # Sub-second remaining lifetime (0.8s) rounds up to "1s", and long call: tag is UTF-8 bounded.
        subsec = await self._send_one(
            _raw(
                room_id="!" + ("é" * 200) + ":ex",
                content=_ring(self.now_ms - 29_200, 30_000, "audio"),
                tweaks={"sound": "ring"},
            )
        )
        self.assertEqual(subsec["android"]["ttl"], "1s")
        call_tag = subsec["android"]["notification"]["tag"]
        self.assertTrue(call_tag.startswith("call:!"))
        self.assertLessEqual(len(call_tag.encode("utf-8")), MAX_THREAD_ID_BYTES)

        video = await self._send_one(
            _raw(
                event_type=ENCRYPTED_EVENT_TYPE,
                sender_display_name=None,
                room_name="Standup",
                room_id=None,
                content=_enc(_ring(self.now_ms, 600_000, "video")),
                tweaks={"sound": "ring"},
            )
        )
        self.assertEqual(video["notification"], {"title": "Standup", "body": "Incoming video call"})
        self.assertEqual(video["android"]["ttl"], "120s")
        self.assertNotIn("tag", video["android"]["notification"])
        self.assertEqual(
            json.loads(video["data"][RING_PAYLOAD_KEY]),
            {"expires_at": self.now_ms + MAX_RING_LIFETIME_MS, "call_type": "video"},
        )

        anon = await self._send_one(
            _raw(
                sender=None, sender_display_name=None, room_name=None,
                content=_ring(self.now_ms), tweaks={"sound": "ring"},
            )
        )
        self.assertEqual(anon["notification"]["title"], "MindRoom")

        for label, raw, want_chan in (
            ("no_ring_tweak_audible", _raw(content=_ring(self.now_ms)), FCM_MESSAGE_CHANNEL_ID),
            ("no_ring_tweak_silent", _raw(content=_ring(self.now_ms), tweaks=None), FCM_SILENT_MESSAGE_CHANNEL_ID),
            (
                "future_skew",
                _raw(content=_ring(self.now_ms + MAX_CLOCK_SKEW_MS + 1), tweaks={"sound": "ring"}),
                FCM_MESSAGE_CHANNEL_ID,
            ),
        ):
            with self.subTest(non_ring=label):
                msg = await self._send_one(raw)
                self.assertEqual(msg["android"]["notification"]["channel_id"], want_chan)
                self.assertEqual(msg["android"]["notification"]["tag"], "!room:example.com")
                self.assertNotIn(RING_PAYLOAD_KEY, msg["data"])

        self.sent_requests.clear()
        self.assertEqual(
            await self._dispatch(_raw(content=_ring(self.now_ms - 30_000), tweaks={"sound": "ring"})), []
        )
        self.assertEqual(self.sent_requests, [])

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

                def _handler(
                    _: Dict[str, Any], st: int = status, hdrs: Any = resp_hdrs, ok: Optional[int] = ok_on
                ) -> _FakeHttpResponse:
                    nonlocal attempts
                    attempts += 1
                    if ok is not None and attempts >= ok:
                        return _FakeHttpResponse(200)
                    return _FakeHttpResponse(st, b"busy", hdrs)

                self.response_handler = _handler
                raw = _raw(content=_ring(self.now_ms - age_ms), tweaks={"sound": "ring"})
                self.assertEqual(await self._dispatch(raw, advance=adv), [])
                self.assertEqual([req[4]["message"]["android"]["ttl"] for req in self.sent_requests], want_ttls)

        self.response_handler = lambda _: _FakeHttpResponse(404, b"unregistered")
        self.assertEqual(await self._dispatch(_msg("hi")), ["fcm-device-token-123"])

    async def test_fcm_hygiene_surrogates_and_bounded_size_trimming(self) -> None:
        msg = await self._send_one(
            _raw(
                event_type=ENCRYPTED_EVENT_TYPE,
                room_id=None,
                content={"algorithm": "m.megolm.v1.aes-sha2"},
                default_payload=FCM_SPOOFED_DEFAULT_PAYLOAD,
            )
        )
        for key in (
            "sender", "sender_display_name", "room_name", RING_PAYLOAD_KEY, PLAIN_PAYLOAD_KEY,
            "room_id", "notification", "android", "from", "message_type",
            "google.c.a.e", "gcm.notification.title", "bad_int", "bad_dict", "bad_none",
        ):
            self.assertNotIn(key, msg["data"])
        self.assertEqual(msg["data"]["event_id"], "$event1")
        self.assertEqual(msg["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")
        self.assertEqual(msg["data"]["custom"], "kept")

        for bad_acct in ("", "   ", 123, None, "a\nb", "a\ud83db", "a" * (MAX_ACCOUNT_BYTES + 1)):
            with self.subTest(bad_account=bad_acct):
                m = await self._send_one(_msg("hi", default_payload={FCM_ACCOUNT_KEY: bad_acct}))
                self.assertNotIn(FCM_ACCOUNT_KEY, m["data"])

        bad = "\ud83d"
        surrogate_msg = await self._send_one(
            _msg(
                f"He{bad}llo", sender=f"@al{bad}ice:ex", sender_display_name=f"Al{bad}ice",
                room_name=f"Eng{bad}", room_id=f"!r{bad}:ex", default_payload={"custom": f"val{bad}"},
            )
        )
        json.dumps(surrogate_msg, ensure_ascii=False).encode("utf-8")
        self.assertIn("\ufffd", surrogate_msg["notification"]["body"])

        # Many custom keys are dropped in ONE single batch (O(1) serializations, never quadratic).
        many_custom = {f"k{i}": "v" * 20 for i in range(400)}
        many_custom[FCM_ACCOUNT_KEY] = "@me:mindroom.chat"
        with patch.object(
            mindroom_fcm_pushkin, "_fcm_wire_size", wraps=mindroom_fcm_pushkin._fcm_wire_size
        ) as size_spy:
            trimmed = await self._send_one(_msg("Short preview", default_payload=many_custom))
        self.assertLessEqual(size_spy.call_count, 3)
        self.assertEqual(trimmed["notification"]["body"], "Short preview")
        self.assertEqual(trimmed["data"][FCM_ACCOUNT_KEY], "@me:mindroom.chat")
        self.assertNotIn("k0", trimmed["data"])

        huge_emoji = await self._send_one(
            _msg("🔥" * 500, default_payload={FCM_ACCOUNT_KEY: "@me:mindroom.chat"})
        )
        self.assertTrue(huge_emoji["notification"]["body"].endswith("…"))
        self.assertGreaterEqual(len(huge_emoji["notification"]["body"].encode("utf-8")), MIN_PREVIEW_BYTES)

        # Unfixably oversized fixed fcm_options (> 4096 bytes) drops cleanly without sending HTTP 400.
        unfixable = await self._await(
            self.sygnal._make_pushkin(
                FCM_APP_ID, {**self.fcm_app_cfg, "fcm_options": {"android": {"collapse_key": "k" * 5000}}}
            )
        )
        self.sent_requests.clear()
        self.assertEqual(await self._dispatch(_msg("hi"), pushkin=unfixable), [])
        self.assertEqual(self.sent_requests, [])

    async def test_safe_fcm_loader_and_legacy_version(self) -> None:
        loader_cfg = {**self.fcm_app_cfg, "type": "mindroom_pushkin_loader.SafeFcmPushkin"}
        self.assertIsInstance(await self._await(self.sygnal._make_pushkin(FCM_APP_ID, loader_cfg)), MindroomFcmPushkin)

        for legacy_cfg in ({"api_version": "legacy", "api_key": "k"}, {"api_key": "k"}):
            direct_cfg = {**legacy_cfg, "type": "mindroom_fcm_pushkin.MindroomFcmPushkin"}
            safe_cfg = {**legacy_cfg, "type": "mindroom_pushkin_loader.SafeFcmPushkin"}
            with self.assertRaises(PushkinSetupException):
                await self._await(self.sygnal._make_pushkin(FCM_APP_ID, direct_cfg))
            fb_legacy = await self._await(self.sygnal._make_pushkin(FCM_APP_ID, safe_cfg))
            self.assertIs(type(fb_legacy), GcmPushkin)

        real_import = importlib.import_module
        for exc in (ImportError("broken"), SyntaxError("bad")):
            def _fail(name: str, *a: Any, e: Exception = exc, **kw: Any) -> Any:
                if name == "mindroom_fcm_pushkin":
                    raise e
                return real_import(name, *a, **kw)

            with patch("importlib.import_module", side_effect=_fail):
                self.assertIs(type(await self._await(self.sygnal._make_pushkin(FCM_APP_ID, loader_cfg))), GcmPushkin)

        with patch.object(MindroomFcmPushkin, "__init__", side_effect=RuntimeError("boom")):
            self.assertIs(type(await self._await(self.sygnal._make_pushkin(FCM_APP_ID, loader_cfg))), GcmPushkin)


if __name__ == "__main__":
    unittest.main()
