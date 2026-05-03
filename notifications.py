"""
Notifications — lightweight heartbeats and error pushes.

Two independent channels:

  1. healthchecks.io "dead-man's switch" pings. If a configured URL stops
     getting pinged, healthchecks.io emails/notifies you. Used for things
     that *should* be happening but might silently stop (ring buffer,
     incoming webhooks).

  2. ntfy.sh push notifications. Used for *active* error pushes —
     something failed right now and you should know.

Both channels are optional: if the URL is empty/None, the function is a
no-op. All network calls fail silently (logged at debug) so a flaky
notification service can never break the pipeline.
"""

import logging

import requests

log = logging.getLogger("notifications")

_TIMEOUT_S = 5


def ping_healthcheck(url: str | None, fail: bool = False) -> None:
    """Send a heartbeat (or failure signal) to a healthchecks.io URL."""
    if not url:
        return
    target = url.rstrip("/") + "/fail" if fail else url
    try:
        requests.get(target, timeout=_TIMEOUT_S)
    except requests.RequestException as e:
        log.debug(f"Healthcheck ping failed ({target}): {e}")


def push_ntfy(
    topic_url: str | None,
    title: str,
    message: str,
    priority: str = "default",
    tags: list[str] | None = None,
) -> None:
    """Send a push notification via ntfy.sh (or a self-hosted instance)."""
    if not topic_url:
        return
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        requests.post(
            topic_url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as e:
        log.debug(f"ntfy push failed ({topic_url}): {e}")
