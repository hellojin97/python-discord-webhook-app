import logging
from typing import Any

import httpx

_EVENT_COLORS: dict[str, int] = {
    "jobs.on_success": 0x2ECC71,
    "jobs.on_failure": 0xE74C3C,
    "jobs.on_start": 0x3498DB,
    "jobs.on_duration_warning_threshold_exceeded": 0xF1C40F,
    "jobs.on_streaming_backlog_exceeded": 0xF1C40F,
}
_DEFAULT_COLOR = 0x95A5A6

_log = logging.getLogger(__name__)


def build_discord_payload(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event", "unknown")
    job_name = event.get("job_name") or event.get("job_id") or "(unknown job)"

    fields: list[dict[str, Any]] = []
    for label, key in (("Run ID", "run_id"), ("Workspace", "workspace_id"), ("Task", "task_name")):
        value = event.get(key)
        if value:
            fields.append({"name": label, "value": str(value), "inline": True})
    
    embed: dict[str, Any] = {
        "title": f"[{event_type}] {job_name}",
        "color": _EVENT_COLORS.get(event_type, _DEFAULT_COLOR),
        "fields": fields
    }

    if run_url := event.get("run_url"):
        embed["url"] = run_url
    if event_time := event.get("event_time"):
        embed["timestamp"] = event_time

    return {"embeds": [embed]}


def send_to_discord(webhook_url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(webhook_url, json=payload)
    resp.raise_for_status()
    _log.info("Discord webhook delivered: status=%d", resp.status_code)