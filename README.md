# mindroom-sygnal

APNs pushkin add-on for [Sygnal](https://github.com/element-hq/sygnal) `v0.17.0` providing rich iOS notification formatting (structured alerts, Markdown stripping, thread grouping, and Notification Service Extension metadata) alongside high-priority MindRoom incoming-call alerts with custom ringtone (`ring.wav`) and deadline-bounded expiry.

## What it does

- Subclasses Sygnal `v0.17.0`'s `ApnsPushkin` as `mindroom_pushkin.MindroomApnsPushkin`.
- **Rich iOS notifications** for alert-bearing non-ring events:
  - Replaces `aps.alert` on `m.room.message` (`m.text` and `m.notice`) with structured `title`, optional `subtitle` (when the room name differs from the sender), and Markdown-stripped `body` (dropping any `default_payload.aps.alert` keys), falling back to stock `MSG_FROM_USER` / `MSG_FROM_USER_IN_ROOM` if the cleaned body is empty.
  - Strips Markdown (including Matrix rich-reply fallbacks and MindRoom tool-call marker lines) from text, notice, and `m.emote` bodies while preserving code blocks and identifiers.
  - Sets `aps.mutable-content = 1`, `aps.thread-id` from `room_id`, and UTF-8-bounded top-level `sender`, `sender_display_name`, and `room_name` metadata for the iOS `UNNotificationServiceExtension` (overwriting or clearing caller-supplied values).
- **Incoming call rings** (recognized only when all three hold):
  1. The homeserver set the `sound: "ring"` tweak.
  2. The wire type matches either `org.matrix.msc4075.rtc.notification` (top-level ring fields) or `m.room.encrypted` (cleartext `io.mindroom.rtc.notification` hint).
  3. `notification_type == "ring"` with valid integer `sender_ts > 0`, `lifetime > 0`, and `sender_ts <= now + 20_000`.
  - Rewrites `aps.alert` to `VOICE_CALL_FROM_USER` / `VIDEO_CALL_FROM_USER` with `[sender_display_name]`, sets `aps.sound = "ring.wav"`, enforces APNs high priority (`10`), caps lifetime at 120 s, adds `mindroom_ring.expires_at`, sets `time_to_live` from remaining ring lifetime, and drops expired rings at payload build time and before every APNs attempt (including retries).
- Leaves badge-only notifications and `event_id_only` payloads identical to stock `ApnsPushkin`.

## Configuration & fallback

Mount the directory containing `mindroom_pushkin.py` and `mindroom_pushkin_loader.py` into the Sygnal `v0.17.0` container (e.g. `/sygnal:ro`), set `PYTHONPATH=/sygnal`, and configure the APNs app with the safe loader:

```yaml
apps:
  chat.mindroom.app:
    type: mindroom_pushkin_loader.SafeApnsPushkin
```

`SafeApnsPushkin.create` imports and constructs `MindroomApnsPushkin`; if importing or constructing it fails for any reason, it logs the traceback and falls back to stock `sygnal.apnspushkin.ApnsPushkin` so ordinary iOS push notifications keep working.

## Nix flake

`packages.<system>.default` builds a store directory containing `mindroom_pushkin.py` and `mindroom_pushkin_loader.py` (`x86_64-linux`, `aarch64-linux`, `aarch64-darwin`, `x86_64-darwin`).

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
