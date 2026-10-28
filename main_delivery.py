"""``deliver_notification``: a routed project notification as a WhatsApp text message.

Notifications are plain text sent to a contact enabled for ``notify``. Approvals through
buttons are out of scope for 0.1.0, so no ``human_decision`` capability is offered and
approval events are not subscribed. Outside the 24-hour window WhatsApp refuses a free-form
message (code 131047); the failure then says so and the delivery is logged as failed.
"""

from __future__ import annotations

from typing import Any

from main_send import select_contact
from main_state import (
    _as_dict,
    _as_str,
    _contact_items_from_config,
    _event_label,
    connection_settings,
)
from main_transport import send_graph_message

WHATSAPP_TEXT_LIMIT = 4096


def _failure(error: str, message: str, *, event_type: str, severity: str) -> dict:
    return {
        "ok": False,
        "delivered": False,
        "error": message,
        "error_code": error,
        "external_effect_status": "failed",
        "definitely_no_external_effect": True,
        "provider_message_id": "",
        "summary": {"event_type": event_type, "severity": severity, "delivered": False},
    }


def _render_message(event: dict, route: dict) -> str:
    severity = _as_str(event.get("severity") or route.get("severity") or "info").upper()
    event_type = _as_str(event.get("event_type") or "unknown")
    payload = _as_dict(event.get("payload"))
    parts = [f"[{severity}] {_event_label(event_type) or event_type}"]
    for key in ("task_summary", "workflow_label", "job_id", "step_label"):
        value = _as_str(payload.get(key))
        if value:
            parts.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    detail = _as_str(event.get("summary") or payload.get("summary") or payload.get("message"))
    if detail:
        parts.append(detail)
    suffix = _as_str(_as_dict(route.get("route_config")).get("message_suffix"))
    if suffix:
        parts.append(suffix)
    return "\n".join(parts)


def _bounded(text: str) -> str:
    # A generated notification (not a step the author wrote) is shortened visibly.
    if len(text) <= WHATSAPP_TEXT_LIMIT:
        return text
    return text[: WHATSAPP_TEXT_LIMIT - 1] + "…"


def _deliver(event: dict, route: dict, provider: dict | None = None) -> dict:
    """Deliver a notification through the Cloud API."""
    event_type = _as_str(event.get("event_type") or "unknown")
    severity = _as_str(route.get("severity") or "info")
    provider_payload = _as_dict(provider)
    config = _as_dict(provider_payload.get("provider_config"))
    token = _as_str(_as_dict(provider_payload.get("provider_secrets")).get("access_token"))
    settings = {**config, **connection_settings(config)}
    channel_id = _as_str(_as_dict(route.get("route_config")).get("channel_id"))
    if not channel_id:
        return _failure(
            "channel_id_required",
            "The route names no contact.",
            event_type=event_type,
            severity=severity,
        )
    contact, failure = select_contact(_contact_items_from_config(config), channel_id)
    if failure is not None or contact is None:
        code = _as_str(_as_dict((failure or ({}, 0))[0]).get("error_code")) or "channel_id_required"
        return _failure(
            code,
            _as_str(_as_dict((failure or ({}, 0))[0]).get("error")) or code,
            event_type=event_type,
            severity=severity,
        )
    message = _bounded(_render_message(event, route))
    response = send_graph_message(
        token,
        settings,
        {
            "recipient_type": "individual",
            "to": _as_str(contact.get("wa_id")),
            "type": "text",
            "text": {"body": message},
        },
    )
    delivered = bool(response.get("ok"))
    result: dict[str, Any] = {
        "ok": delivered,
        "delivered": delivered,
        "error": _as_str(response.get("error")),
        "provider_message_id": _as_str(response.get("message_id")),
        "channel_id": _as_str(contact.get("channel_id")),
        "summary": {"event_type": event_type, "severity": severity, "delivered": delivered},
    }
    for field in (
        "error_code",
        "external_effect_status",
        "definitely_no_external_effect",
        "retryable",
        "failure_class",
    ):
        if field in response:
            result[field] = response[field]
    return result
