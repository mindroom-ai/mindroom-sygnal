# mindroom-sygnal

APNs and FCM HTTP v1 pushkin add-ons for [Sygnal](https://github.com/element-hq/sygnal) `v0.17.0` providing rich iOS/Android notification formatting (structured alerts, Markdown stripping, room grouping, tool-approval summaries, internal/redaction filtering, and iOS NSE metadata) alongside high-priority MindRoom incoming-call alerts with deadline-bounded expiry.

## What it does

### iOS APNs (`mindroom_pushkin.MindroomApnsPushkin`)

- Subclasses Sygnal `v0.17.0`'s `ApnsPushkin`.
- **Rich iOS notifications** for alert-bearing non-ring events:
  - Drops `m.room.redaction` and internal `io.mindroom.*` / `com.mindroom.*` events (except pending `io.mindroom.tool_approval`, which formats as `"Approval needed: <tool_name>"` / `"Approval needed"`).
  - Replaces `aps.alert` on `m.room.message` (`m.text` and `m.notice`) with structured `title`, optional `subtitle` (when the room name differs from the sender), and Markdown-stripped `body` (dropping any `default_payload.aps.alert` keys), falling back to stock `MSG_FROM_USER` / `MSG_FROM_USER_IN_ROOM` if the cleaned body is empty.
  - Strips Markdown (including Matrix rich-reply fallbacks and MindRoom tool-call marker lines) from text, notice, and `m.emote` bodies while preserving code blocks and identifiers, and marks stripped previews with top-level `mindroom_plain: 1` so the iOS extension does not strip them twice.
  - Sets `aps.mutable-content = 1`, `aps.thread-id` from `room_id`, and UTF-8-bounded top-level `sender`, `sender_display_name`, and `room_name` metadata for the iOS `UNNotificationServiceExtension`, while stripping pusher-supplied gateway-owned fields from `default_payload`.
  - Stays within the APNs 4096-byte limit without erasing the preview (dropping optional metadata, then falling back to the sanitized stock payload if needed).
- **Incoming call rings** (recognized only when `tweaks.sound == "ring"`, wire type is `org.matrix.msc4075.rtc.notification` or `m.room.encrypted` with cleartext `io.mindroom.rtc.notification`, and `notification_type == "ring"` with valid `sender_ts > 0`, `lifetime > 0`, and `sender_ts <= now + 20_000`):
  - Rewrites `aps.alert` to `VOICE_CALL_FROM_USER` / `VIDEO_CALL_FROM_USER`, sets `aps.sound = "ring.wav"`, enforces APNs high priority (`10`), caps lifetime at 120 s, adds `mindroom_ring.expires_at`, recomputes `time_to_live` before every send, and drops expired rings at build time and before every retry.
- Leaves badge-only notifications and `event_id_only` payloads identical to stock `ApnsPushkin`.

### Android FCM HTTP v1 (`mindroom_fcm_pushkin.MindroomFcmPushkin`)

- Subclasses Sygnal `v0.17.0`'s `GcmPushkin` (requires `api_version: v1`; `SafeFcmPushkin` falls back to stock `GcmPushkin` on setup failure).
- **Rich visible Android notifications** for alert-bearing events (non-internal, non-redaction, non-badge-only, non-join membership):
  - Populates FCM HTTP v1 `message.notification = {title, body}` using the shared Markdown-stripping and tool-approval helpers (room + sender title for group rooms, sender for DMs, `"New message"` fallback for `event_id_only` and encrypted/bodyless alerts).
  - Sets `message.android.priority` (`normal` for `prio == "low"`, else `high`) and `message.android.notification` with `channel_id: "mindroom_messages"` + `sound: "default"` when the homeserver sent a `sound` tweak, or `channel_id: "mindroom_messages_silent"` without `sound` for silent push rules, plus a UTF-8-bounded `tag` from `room_id` (stripping `title`/`body`/`*_loc_*` overrides from `fcm_options.android.notification`).
- **Incoming call rings** (same 3-part recognition and clock-skew/120 s lifetime rules as APNs):
  - Sent as visible high-priority Android notifications (so the OS displays the alert when the app is backgrounded/killed, and `message.data.mindroom_ring` is consumed by the app on notification tap or when in the foreground).
  - Sets `message.notification` to caller/room title + `"Incoming voice call"` / `"Incoming video call"`, `message.android` with `priority: "high"`, `ttl: "<remaining_secs>s"` (rounded up to at least `1s`), `channel_id: "mindroom_calls"`, `sound: "default"`, optional UTF-8-bounded `tag` prefixed with `"call:"` (so chat messages in the same room do not replace active call banners), and stringified `message.data.mindroom_ring` (`{"expires_at":...,"call_type":"voice"|"video"}`).
  - Drops expired rings at build time and before every FCM attempt (including retries), recomputing `android.ttl` on each attempt.
- **Data hygiene & size limits**:
  - Keeps `message.data` strictly string-valued with routing fields (`room_id`, `event_id`, validated bounded `mindroom_account`, `mindroom_plain: "1"`, `mindroom_ring`), removes `fcm_options.android.data` (merging safe string entries underneath gateway-built `message.data`), strips untrusted gateway-owned and FCM-forbidden keys from `default_payload`, strips any `fcm_options` notification banners on data-only / badge-only pushes, and trims/drops optional metadata in bounded O(1) passes to stay within the 4096-byte `MAX_FIREBASE_MESSAGE_SIZE` wire limit (dropping unfixably oversized payloads rather than sending guaranteed-400 requests).

## Configuration & fallback

Mount the directory containing `mindroom_pushkin.py`, `mindroom_fcm_pushkin.py`, and `mindroom_pushkin_loader.py` into the Sygnal `v0.17.0` container (e.g. `/sygnal:ro`), set `PYTHONPATH=/sygnal`, and configure the apps with the safe loaders:

```yaml
apps:
  chat.mindroom.app:
    type: mindroom_pushkin_loader.SafeApnsPushkin
    # ...standard Sygnal APNs keys (keyfile, key_id, team_id, topic, platform)...

  com.mindroom_ai.app:
    type: mindroom_pushkin_loader.SafeFcmPushkin
    api_version: v1
    project_id: your-firebase-project-id
    service_account_file: /run/secrets/fcm-service-account.json
```

`SafeApnsPushkin.create` and `SafeFcmPushkin.create` import and construct `MindroomApnsPushkin` / `MindroomFcmPushkin`; if importing or constructing them fails for any reason (including non-`v1` FCM config), they log the traceback and fall back to stock `ApnsPushkin` / `GcmPushkin` so pushkin initialization still succeeds (note that Google retired the legacy FCM endpoint, so `api_version: v1` is still required for live FCM delivery).

## Nix flake

`packages.<system>.default` builds a store directory containing `mindroom_pushkin.py`, `mindroom_fcm_pushkin.py`, and `mindroom_pushkin_loader.py` (`x86_64-linux`, `aarch64-linux`, `aarch64-darwin`, `x86_64-darwin`).

## Testing

Run the test suite inside the pinned Sygnal `v0.17.0` image:

```bash
docker run --rm --network=none --entrypoint python \
  -e PYTHONPATH=/sygnal -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD:/sygnal:ro" \
  docker.io/matrixdotorg/sygnal@sha256:141f5e72fdf99a28e29ca87f3a46c19ed1cf21d5074aa3ef9df7c067e0c1c46a \
  -m unittest discover -s /sygnal -p "test_*.py" -v
```

## License

Licensed under the GNU Affero General Public License v3.0 (`AGPL-3.0-only`), matching upstream Sygnal `v0.17.0`. See [`LICENSE`](./LICENSE).
