"""Inbound WhatsApp webhooks (the ``ingress_event`` action).

WhatsApp has no polling: traffic arrives only as webhooks. Core verifies Meta's signature
(``X-Hub-Signature-256``, HMAC-SHA256 of the raw body keyed with the app secret) and
answers Meta's GET handshake before this action runs, so ``ingress_event`` receives only
payloads Core already authenticated (``callback.authenticated``). Meta retries a failed
delivery for up to 7 days, so every message and status is deduplicated: by the message ID
(wamid) for a message and by ``wamid:status`` for a status.

A message becomes a ``whatsapp.message.received`` event only when its sender is a saved,
enabled contact with ``capabilities.workflow_trigger`` (closed by default). Every sender is
remembered as a discovered sender, so the owner can save it as a contact. Statuses become
``whatsapp.message.status`` events; Core filters them by the trigger's configuration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    _as_dict,
    _as_list,
    _as_str,
    _coerce_bool,
    _coerce_int,
    _coerce_project_id,
    _contact_items_from_config,
    _discovered_sender_row,
    _normalize_inbound_state,
    _runtime_logger,
    _save_connection,
    _upsert_discovered_sender,
    connection_settings,
    normalize_wa_id,
)

MESSAGE_RECEIVED_EVENT = "whatsapp.message.received"
MESSAGE_STATUS_EVENT = "whatsapp.message.status"
_MEDIA_TYPES = ("image", "video", "document", "audio")
_SEEN_KEYS_LIMIT = 500


def _workflow_trigger_contact_for_sender(
    contacts: list[dict[str, Any]], wa_id: str
) -> dict[str, Any]:
    """The enabled contact of this sender that may start workflows, else ``{}``.

    Closed by default: an unknown sender, a disabled contact and a contact without
    ``capabilities.workflow_trigger`` all start nothing.
    """
    sender = normalize_wa_id(wa_id)
    if not sender:
        return {}
    for contact in contacts:
        if normalize_wa_id(contact.get("wa_id")) != sender:
            continue
        if not _coerce_bool(contact.get("enabled"), True):
            continue
        if _coerce_bool(_as_dict(contact.get("capabilities")).get("workflow_trigger"), False):
            return contact
    return {}


def _timestamp(value: Any) -> int:
    return _coerce_int(value) or 0


def _message_fields(message: dict[str, Any]) -> dict[str, Any]:
    """The type-specific part of the event payload for one inbound message."""
    message_type = _as_str(message.get("type")) or "unknown"
    fields: dict[str, Any] = {
        "message_type": message_type,
        "text": "",
        "has_file": False,
        "file_id": "",
        "file_kind": "",
        "file_name": "",
        "mime_type": "",
        "caption": "",
        "button_id": "",
        "button_title": "",
    }
    if message_type == "text":
        fields["text"] = _as_str(_as_dict(message.get("text")).get("body"))
    elif message_type in _MEDIA_TYPES:
        media = _as_dict(message.get(message_type))
        media_id = _as_str(media.get("id"))
        fields.update(
            {
                "has_file": bool(media_id),
                "file_id": media_id,
                "file_kind": message_type if media_id else "",
                "file_name": _as_str(media.get("filename")),
                "mime_type": _as_str(media.get("mime_type")),
                "caption": _as_str(media.get("caption")),
                "text": _as_str(media.get("caption")),
            }
        )
        if message_type == "audio":
            fields["is_voice"] = _coerce_bool(media.get("voice"), False)
    elif message_type == "interactive":
        interactive = _as_dict(message.get("interactive"))
        kind = _as_str(interactive.get("type"))
        reply = _as_dict(interactive.get(kind)) if kind in {"button_reply", "list_reply"} else {}
        fields["interactive_type"] = kind
        fields["button_id"] = _as_str(reply.get("id"))
        fields["button_title"] = _as_str(reply.get("title"))
        fields["text"] = _as_str(reply.get("title"))
        if kind == "list_reply":
            fields["list_description"] = _as_str(reply.get("description"))
    elif message_type == "location":
        location = _as_dict(message.get("location"))
        fields["location_latitude"] = location.get("latitude")
        fields["location_longitude"] = location.get("longitude")
        fields["location_name"] = _as_str(location.get("name"))
        fields["location_address"] = _as_str(location.get("address"))
    elif message_type == "reaction":
        reaction = _as_dict(message.get("reaction"))
        fields["reaction_message_id"] = _as_str(reaction.get("message_id"))
        # The emoji is omitted when the user removes a reaction.
        fields["reaction_emoji"] = _as_str(reaction.get("emoji"))
    return fields


def _message_received_event(
    message: dict[str, Any],
    *,
    value: dict[str, Any],
    profile_name: str,
    project_id: str | None,
    connection_id: str,
) -> dict[str, Any]:
    wa_id = _as_str(message.get("from"))
    message_id = _as_str(message.get("id"))
    payload: dict[str, Any] = {
        **_message_fields(message),
        "wa_id": wa_id,
        "profile_name": profile_name,
        "message_id": message_id,
        "reply_to_message_id": _as_str(_as_dict(message.get("context")).get("id")),
        "timestamp": _timestamp(message.get("timestamp")),
        "phone_number_id": _as_str(_as_dict(value.get("metadata")).get("phone_number_id")),
        # Stable per message, so a Meta retry never starts a second run.
        "dedupe_key": message_id,
    }
    if project_id:
        payload["project_id"] = project_id
    if connection_id:
        payload["connection_id"] = connection_id
    sender = f"{profile_name} ({wa_id})" if profile_name else wa_id or "someone"
    event: dict[str, Any] = {
        "event_type": MESSAGE_RECEIVED_EVENT,
        "severity": "info",
        "summary": f"WhatsApp message from {sender}",
        "correlation_id": message_id,
        "source": "whatsapp.message",
        "payload": payload,
    }
    if project_id:
        event["project_id"] = project_id
    return event


def _message_status_event(
    status: dict[str, Any],
    *,
    value: dict[str, Any],
    project_id: str | None,
    connection_id: str,
) -> dict[str, Any]:
    message_id = _as_str(status.get("id"))
    state = _as_str(status.get("status")).lower()
    payload: dict[str, Any] = {
        "status": state,
        "message_id": message_id,
        "wa_id": _as_str(status.get("recipient_id")),
        "recipient_id": _as_str(status.get("recipient_id")),
        "timestamp": _timestamp(status.get("timestamp")),
        "phone_number_id": _as_str(_as_dict(value.get("metadata")).get("phone_number_id")),
        "dedupe_key": f"{message_id}:{state}",
    }
    errors = [_as_dict(item) for item in _as_list(status.get("errors"))]
    if errors:
        first = errors[0]
        payload["error_code"] = _coerce_int(first.get("code")) or 0
        payload["error_title"] = _as_str(first.get("title"))
        payload["error_message"] = _as_str(first.get("message"))
        payload["error_details"] = _as_str(_as_dict(first.get("error_data")).get("details"))
    if project_id:
        payload["project_id"] = project_id
    if connection_id:
        payload["connection_id"] = connection_id
    event: dict[str, Any] = {
        "event_type": MESSAGE_STATUS_EVENT,
        "severity": "warning" if state == "failed" else "info",
        "summary": f"WhatsApp message {message_id} is {state}",
        "correlation_id": payload["dedupe_key"],
        "source": "whatsapp.status",
        "payload": payload,
    }
    if project_id:
        event["project_id"] = project_id
    return event


def _provider_runtime(provider: dict[str, Any]) -> dict[str, Any]:
    config = _as_dict(provider.get("provider_config"))
    return {
        "config": config,
        "settings": connection_settings(config),
        "contacts": _contact_items_from_config(config),
        "ingress_config": _as_dict(provider.get("ingress_config")),
    }


def _ingress_event_response(request_payload: dict[str, Any]) -> dict[str, Any]:
    callback_info = _as_dict(request_payload.get("callback"))
    if not _coerce_bool(callback_info.get("authenticated"), False):
        return {
            "ok": False,
            "error": "unauthorized_whatsapp_ingress",
            "result": {"provider": "whatsapp", "ingress_type": "unauthorized"},
            "events": [],
        }
    method = _as_str(request_payload.get("method")).upper()
    payload = _as_dict(request_payload.get("payload") or request_payload.get("json"))
    if method == "GET" or payload.get("object") != "whatsapp_business_account":
        return {
            "ok": True,
            "result": {"provider": "whatsapp", "ingress_type": "ignored", "ignored": True},
            "events": [],
        }
    provider = _as_dict(request_payload.get("provider"))
    runtime = _provider_runtime(provider)
    project_id = _coerce_project_id(
        callback_info.get("project_id"),
        provider.get("project_id"),
        _as_dict(runtime["config"]).get("project_id"),
    )
    connection_id = _as_str(callback_info.get("connection_id") or provider.get("connection_id"))
    configured_number = _as_str(runtime["settings"].get("phone_number_id"))
    state = _normalize_inbound_state(runtime["ingress_config"])
    seen = list(state["seen_message_keys"])
    discovered = list(state["discovered_senders"])
    events: list[dict[str, Any]] = []
    counts = {"messages": 0, "statuses": 0, "duplicates": 0, "not_allowed": 0, "other_number": 0}
    changed = False

    for entry in _as_list(payload.get("entry")):
        for change in _as_list(_as_dict(entry).get("changes")):
            change_payload = _as_dict(change)
            if _as_str(change_payload.get("field")) not in {"", "messages"}:
                continue
            value = _as_dict(change_payload.get("value"))
            number = _as_str(_as_dict(value.get("metadata")).get("phone_number_id"))
            # Fail closed: a connection without its phone number id is not bound to a
            # business number, so it must not take messages addressed to any number.
            if not configured_number or number != configured_number:
                counts["other_number"] += 1
                continue
            profiles = {
                _as_str(_as_dict(c).get("wa_id")): _as_str(
                    _as_dict(_as_dict(c).get("profile")).get("name")
                )
                for c in _as_list(value.get("contacts"))
            }
            for raw_message in _as_list(value.get("messages")):
                message = _as_dict(raw_message)
                message_id = _as_str(message.get("id"))
                wa_id = _as_str(message.get("from"))
                if not message_id or not wa_id:
                    continue
                if message_id in seen:
                    counts["duplicates"] += 1
                    continue
                seen.append(message_id)
                changed = True
                profile_name = profiles.get(wa_id, "")
                discovered = _upsert_discovered_sender(
                    discovered,
                    _discovered_sender_row(
                        wa_id=wa_id,
                        profile_name=profile_name,
                        last_message_type=_as_str(message.get("type")),
                    ),
                )
                if not _workflow_trigger_contact_for_sender(runtime["contacts"], wa_id):
                    counts["not_allowed"] += 1
                    continue
                counts["messages"] += 1
                events.append(
                    _message_received_event(
                        message,
                        value=value,
                        profile_name=profile_name,
                        project_id=project_id,
                        connection_id=connection_id,
                    )
                )
            for raw_status in _as_list(value.get("statuses")):
                status = _as_dict(raw_status)
                message_id = _as_str(status.get("id"))
                state_name = _as_str(status.get("status")).lower()
                if not message_id or not state_name:
                    continue
                key = f"{message_id}:{state_name}"
                if key in seen:
                    counts["duplicates"] += 1
                    continue
                seen.append(key)
                changed = True
                counts["statuses"] += 1
                events.append(
                    _message_status_event(
                        status, value=value, project_id=project_id, connection_id=connection_id
                    )
                )

    if changed and project_id:
        next_state = {
            **runtime["ingress_config"],
            "discovered_senders": discovered,
            "seen_message_keys": seen[-_SEEN_KEYS_LIMIT:],
            "last_inbound_at": datetime.now(UTC).isoformat(),
        }
        bl = _runtime_logger(_REQUEST_RUNTIME_CONTEXT)
        if bl is not None:
            _save_connection(bl, str(project_id), ingress_config=next_state)
    result: dict[str, Any] = {"provider": "whatsapp", "ingress_type": "webhook", **counts}
    if project_id:
        result["project_id"] = project_id
    return {"ok": True, "result": result, "events": events}


def _handle_ingress_event_payload(action_payload: dict[str, Any]) -> tuple[dict, int]:
    request_payload = _as_dict(action_payload.get("request"))
    if not request_payload:
        input_payload = _as_dict(action_payload.get("input"))
        request_payload = _as_dict(input_payload.get("request")) if input_payload else {}
        callback_payload = _as_dict(input_payload.get("callback")) if input_payload else {}
        if callback_payload and "callback" not in request_payload:
            request_payload = {**request_payload, "callback": callback_payload}
    elif "callback" not in request_payload:
        callback_payload = _as_dict(action_payload.get("callback"))
        if callback_payload:
            request_payload = {**request_payload, "callback": callback_payload}
    runtime_context = _as_dict(action_payload.get("runtime_context")) or _REQUEST_RUNTIME_CONTEXT
    runtime_resources = _as_dict(runtime_context.get("resources"))
    project_connection = _as_dict(runtime_resources.get("project_connection"))
    provider_secrets = _as_dict(runtime_resources.get("provider_secrets"))
    if project_connection or provider_secrets:
        provider = _as_dict(request_payload.get("provider"))
        connection_config = _as_dict(
            project_connection.get("connection_config") or project_connection.get("config")
        )
        request_payload = {
            **request_payload,
            "provider": {
                **provider,
                "provider_config": connection_config or _as_dict(provider.get("provider_config")),
                "connection_id": _as_str(project_connection.get("connection_id")),
                "project_id": _coerce_project_id(
                    project_connection.get("project_id"), provider.get("project_id")
                ),
                "ingress_config": _as_dict(project_connection.get("ingress_config"))
                or _as_dict(provider.get("ingress_config")),
            },
        }
    return _ingress_event_response(request_payload), 0
