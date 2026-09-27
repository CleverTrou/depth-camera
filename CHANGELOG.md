<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

# Changelog

All notable changes to depth-camera are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-09-25

### Added

- The relay pings its healthcheck on a fixed schedule (default every 12 hours),
  not only when a webhook arrives. A quiet night no longer looks like a dead relay.
  ([#48](https://github.com/CleverTrou/depth-camera/pull/48))
- Hash-pinned `requirements.lock` for supply-chain protection.

### Changed

- depth-relay and depth-gallery bind dual-stack (`::`), so they answer over IPv6
  on the tailnet. ([#41](https://github.com/CleverTrou/depth-camera/pull/41))
- Minimum dependency versions raised: requests 2.34.2, onnxruntime 1.27,
  matplotlib 3.11, pillow 12.3, numpy 2.5.

### Fixed

- Every depth run failed on matplotlib 3.9 and later, because
  `matplotlib.cm.get_cmap()` was removed there.
  ([#58](https://github.com/CleverTrou/depth-camera/pull/58))

## [0.4.0] - 2026-05-19

### Added

- Motion diff images for Pi Monitor events, with a display threshold to cut
  noise. ([#19](https://github.com/CleverTrou/depth-camera/pull/19),
  [#28](https://github.com/CleverTrou/depth-camera/pull/28))
- Settings page: camera FOV detector, point-cloud controls (including
  "Regenerate all PLY files"), and motion-detection tuning.
  ([#30](https://github.com/CleverTrou/depth-camera/pull/30),
  [#31](https://github.com/CleverTrou/depth-camera/pull/31))
- Five UI modes: Glass, Instrument, Cinematic, Brutalist, and Soft.
  ([#33](https://github.com/CleverTrou/depth-camera/pull/33))
- WebXR support via `xr-bridge.js`, plus a responsive layout pass.
  ([#36](https://github.com/CleverTrou/depth-camera/pull/36))
- Gallery pagination, with sequence numbers that stay stable across pages and
  timestamps that show seconds.
  ([#27](https://github.com/CleverTrou/depth-camera/pull/27),
  [#29](https://github.com/CleverTrou/depth-camera/pull/29))
- A configurable `ply_depth_scale`.
  ([#26](https://github.com/CleverTrou/depth-camera/pull/26))

### Changed

- Motion detection compares against an EWMA background model instead of the
  previous frame. ([#24](https://github.com/CleverTrou/depth-camera/pull/24))
- Gallery filters persist between visits.
  ([#21](https://github.com/CleverTrou/depth-camera/pull/21))

### Fixed

- Point clouds bowed at the ground plane. The camera FOV is corrected and RANSAC
  plane fitting added. ([#25](https://github.com/CleverTrou/depth-camera/pull/25))
- Frame extraction on ffmpeg 7.x (`-skip_frame nokey`).
  ([#18](https://github.com/CleverTrou/depth-camera/pull/18))
- Smearing in motion comparison frames and ring-buffer extraction.
  ([#20](https://github.com/CleverTrou/depth-camera/pull/20))
- Scroll position on reload, the viewer's diff fallback, and gallery refresh
  when the tab becomes visible again.
  ([#21](https://github.com/CleverTrou/depth-camera/pull/21),
  [#23](https://github.com/CleverTrou/depth-camera/pull/23))
- Saving settings no longer blocks while services restart.
  ([#32](https://github.com/CleverTrou/depth-camera/pull/32))
- VR browsers: a cycle button replaces the UI-mode dropdown, and there's an HTTPS
  hint for WebXR. ([#35](https://github.com/CleverTrou/depth-camera/pull/35))
- XR: the Settings page and point-cloud rendering.
  ([#37](https://github.com/CleverTrou/depth-camera/pull/37))
- The Settings page rendered blank.

## [0.3.0] - 2026-05-13

### Added

- Pi Monitor events in the gallery behind a Source toggle, with per-source
  event caps. ([#2](https://github.com/CleverTrou/depth-camera/pull/2))
- Motion-detection telemetry for tuning from real data, and a motion-type chip
  on events. ([#4](https://github.com/CleverTrou/depth-camera/pull/4),
  [#5](https://github.com/CleverTrou/depth-camera/pull/5))
- Events are numbered chronologically within each day.
  ([#7](https://github.com/CleverTrou/depth-camera/pull/7))
- Retry on transient capture failures.
  ([#8](https://github.com/CleverTrou/depth-camera/pull/8))

### Changed

- **Breaking:** The ntfy topic and healthcheck URLs come from environment
  variables (`NTFY_TOPIC_ALERTS`, `HEALTHCHECK_*_URL`), not `config.yaml`.
- The IFTTT gallery shows only IFTTT events, and segment concatenation is wider
  so extraction always finds a keyframe.
  ([#1](https://github.com/CleverTrou/depth-camera/pull/1))
- The gallery layout works on mobile, and the viewer opens in a more sensible
  default mode. ([#6](https://github.com/CleverTrou/depth-camera/pull/6))

### Fixed

- ffmpeg `EINVAL` on stream-end seeks and when the camera wakes up, and
  smearing from reading RTSP directly.
  ([#3](https://github.com/CleverTrou/depth-camera/pull/3),
  [#8](https://github.com/CleverTrou/depth-camera/pull/8))
- Motion-detection noise and false triggers on busy scenes.
  ([#5](https://github.com/CleverTrou/depth-camera/pull/5),
  [#9](https://github.com/CleverTrou/depth-camera/pull/9))

### Security

- Gitleaks and TruffleHog secret scanning in CI, and Dependabot.

## [0.2.0] - 2026-05-03

### Added

- A redesigned gallery, and a unified viewer with four modes: parallax, depth,
  point cloud, and XR.
- healthchecks.io heartbeats and ntfy error pushes.

### Changed

- Services run as the invoking user, detected via `$SUDO_USER`, not a
  hard-coded `pi`.

### Fixed

- Point cloud orientation.
- The iOS gyroscope permission prompt shows over both HTTP and HTTPS.

### Security

- A PIN protects the gallery, and `event_id` is validated.
- Privacy pass before making the repository public.

## [0.1.0] - 2026-04-15

First working version.

### Added

- Pipeline for Raspberry Pi 5: an RTSP ring buffer records the Aqara camera, an
  IFTTT webhook marks events, Depth Anything V2 estimates depth, and a gallery
  shows the result with a gyroscope parallax viewer.
- Event types taken from the URL path, and exact event timestamps for precise
  lookback.
- Badge colors for vehicle and lingering-person events.

### Changed

- Camera credentials live in an environment file, and Tailscale Funnel replaces
  port forwarding.

### Fixed

- Smeared frames. Extraction uses output seeking, skips the segment still being
  written, and concatenates segments.
- The parallax viewer's aspect ratio.

[Unreleased]: https://github.com/CleverTrou/depth-camera/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/CleverTrou/depth-camera/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/CleverTrou/depth-camera/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/CleverTrou/depth-camera/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/CleverTrou/depth-camera/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/CleverTrou/depth-camera/tree/v0.1.0
