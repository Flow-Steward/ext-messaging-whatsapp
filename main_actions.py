from __future__ import annotations

import contextlib
from typing import Any

from main_delivery import _deliver
from main_ingress import _handle_ingress_event_payload
from main_readiness import MESSAGES, classify_api_failure
from main_send import (
    handle_mark_as_read,
    handle_send_message,
    handle_send_reaction,
    handle_send_template,
)
from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    PROVIDER_KEY,
    SECRET_ACCESS_TOKEN,
    _as_dict,
    _as_list,
    _as_str,
    _callback_registration_for_project,
    _connection_row_for_project,
    _contact_items_for_ui,
    _contact_items_from_config,
    _coerce_project_id,
    _normalize_inbound_state,
    _replace_contacts_in_config,
    _runtime_logger,
    _save_connection,
    _slugify_contact_id,
    connection_settings,
    normalize_wa_id,
)
from main_steps import handle_get_file, handle_send_file
from main_transport import _retry_error_message, _retryable_delivery_status, get_phone_number

# Workflow steps; each gets everything it needs in the action.
_STEP_HANDLERS = {
    "send_message": handle_send_message,
    "send_template": handle_send_template,
    "send_reaction": handle_send_reaction,
    "mark_as_read": handle_mark_as_read,
    "send_file": handle_send_file,
    "get_file": handle_get_file,
}


def _token_for(bl: Any, project_id: Any, override: dict[str, Any] | None = None) -> str:
    token = _as_str(_as_dict(override).get(SECRET_ACCESS_TOKEN))
    if token or bl is None or not project_id or not hasattr(bl, "get_provider_secret"):
        return token
    try:
        return _as_str(
            bl.get_provider_secret(
                provider_key=PROVIDER_KEY,
                secret_key=SECRET_ACCESS_TOKEN,
                project_id=str(project_id),
                include_global=False,
            )
        )
    except Exception:
        return ""


def _provider_for(config: dict[str, Any], token: str) -> dict[str, Any]:
    return {"provider_config": config, "provider_secrets": {SECRET_ACCESS_TOKEN: token}}


def _handle_action(action: dict) -> tuple[dict, int]:
    action_id = str(action.get("action_id") or "").strip()
    context = _as_dict(action.get("context"))
    target = _as_dict(action.get("target"))
    input_payload = _as_dict(action.get("input"))
    project_id = _coerce_project_id(
        context.get("project_id"), target.get("project_id"), input_payload.get("project_id")
    )
    runtime_context = dict(_as_dict(action.get("runtime_context")) or _REQUEST_RUNTIME_CONTEXT)
    if target and "target" not in runtime_context:
        runtime_context["target"] = target
    step_handler = _STEP_HANDLERS.get(action_id)
    if step_handler is not None:
        return step_handler(action, runtime_context)
    bl = _runtime_logger(runtime_context)
    connection = (
        _connection_row_for_project(bl, str(project_id)) if (bl is not None and project_id) else {}
    )
    config = _as_dict(connection.get("connection_config"))

    if action_id == "deliver_notification":
        target_connection = _as_dict(target.get("connection"))
        if not target_connection:
            return {"ok": False, "error": "project_connection_required"}, 2
        result = _deliver(
            _as_dict(input_payload.get("event")),
            _as_dict(input_payload.get("route")),
            {
                "provider_config": _as_dict(
                    target_connection.get("connection_config") or target_connection.get("config")
                ),
                "provider_secrets": _as_dict(target_connection.get("secrets")),
            },
        )
        return {"ok": bool(result.get("ok")), "result": result}, 0

    if action_id == "retry_delivery":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        item_ids = _as_list(target.get("item_ids"))
        delivery_id = _as_str(item_ids[0] if item_ids else target.get("delivery_id"))
        if not delivery_id:
            return {"ok": False, "error": "invalid_payload"}, 2
        if bl is None or not hasattr(bl, "list_notification_delivery_logs"):
            return {"ok": False, "error": "delivery_retry_unavailable"}, 2
        rows = bl.list_notification_delivery_logs(project_id=str(project_id), limit=2000) or []
        delivery_row = next(
            (
                _as_dict(row)
                for row in rows
                if _as_str(_as_dict(row).get("delivery_id")) == delivery_id
                and _as_str(_as_dict(row).get("provider_key")).lower() == PROVIDER_KEY
            ),
            {},
        )
        if not delivery_row:
            return {"ok": False, "error": "delivery_not_found"}, 2
        original_status = _as_str(delivery_row.get("status")).lower()
        if not _retryable_delivery_status(original_status):
            return {
                "ok": False,
                "error": "delivery_not_retryable",
                "message": "Only failed or skipped deliveries can be retried.",
                "reload": ["delivery_grid"],
                "result": {
                    "ok": False,
                    "delivered": False,
                    "error": "delivery_not_retryable",
                    "original_status": original_status,
                },
            }, 0
        request_payload = _as_dict(delivery_row.get("request"))
        event = _as_dict(request_payload.get("event"))
        route_snapshot = _as_dict(request_payload.get("route"))
        if not event or not route_snapshot:
            return {"ok": False, "error": "invalid_payload"}, 2
        result = _deliver(event, route_snapshot, _provider_for(config, _token_for(bl, project_id)))
        status = "delivered" if result.get("delivered") else "failed"
        if hasattr(bl, "log_notification_delivery"):
            with contextlib.suppress(Exception):
                bl.log_notification_delivery(
                    route_id=_as_str(delivery_row.get("route_id")) or None,
                    project_id=str(project_id),
                    workflow_id=_as_str(delivery_row.get("workflow_id")) or None,
                    job_id=_as_str(delivery_row.get("job_id")) or None,
                    provider_key=PROVIDER_KEY,
                    event_type=_as_str(delivery_row.get("event_type"))
                    or _as_str(event.get("event_type")),
                    severity=_as_str(delivery_row.get("severity"))
                    or _as_str(event.get("severity"))
                    or "info",
                    status=status,
                    request=request_payload,
                    response=result,
                    provider_message_id=_as_str(result.get("provider_message_id")) or None,
                    error_text=_as_str(result.get("error")) or None,
                )
        return {
            "ok": bool(result.get("ok")),
            "message": "Retry delivered"
            if result.get("delivered")
            else _retry_error_message(result.get("error_code")),
            "reload": ["delivery_grid", "health_panel"],
            "result": result,
        }, 0

    if action_id == "test_delivery":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        requested = _as_str(input_payload.get("channel_id"))
        if not requested:
            return {
                "ok": False,
                "error": "contact_required",
                "message": MESSAGES["contact_required"],
            }, 2
        result = _deliver(
            {
                "event_type": "test.notification",
                "severity": "info",
                "summary": "Test notification from Flow Steward.",
            },
            {"severity": "info", "route_config": {"channel_id": requested}},
            _provider_for(config, _token_for(bl, project_id)),
        )
        return {
            "ok": bool(result.get("ok")),
            "message": "Test delivered"
            if result.get("delivered")
            else _as_str(result.get("error")) or "Test delivery failed",
            "reload": ["health_panel"],
            "result": result,
        }, 0 if result.get("ok") else 2

    if action_id == "verify_connection":
        token = _token_for(bl, project_id, _as_dict(input_payload.get("provider")))
        if not token:
            return {
                "ok": False,
                "error": "access_token_required",
                "message": MESSAGES["access_token_required"],
                "result": {"verified": False},
            }, 2
        settings = connection_settings(config)
        if not settings["phone_number_id"]:
            return {
                "ok": False,
                "error": "phone_number_id_required",
                "message": MESSAGES["phone_number_id_required"],
                "result": {"verified": False},
            }, 2
        response = get_phone_number(token, settings)
        if response.get("ok"):
            data = _as_dict(response.get("data"))
            number = _as_str(data.get("display_phone_number"))
            name = _as_str(data.get("verified_name"))
            return {
                "ok": True,
                "message": f"Connected as {name} {number}".strip(),
                "result": {
                    "verified": True,
                    "display_phone_number": number,
                    "verified_name": name,
                    "quality_rating": _as_str(data.get("quality_rating")),
                },
                "reload": ["health_panel"],
            }, 0
        failure_code = (
            "access_token_rejected"
            if classify_api_failure(response) == "unauthorized"
            else "whatsapp_unreachable"
        )
        return {
            "ok": False,
            "error": failure_code,
            "message": MESSAGES[failure_code],
            "result": {"verified": False},
        }, 2

    if action_id == "save_discovered_sender":
        if not project_id:
            return {"ok": False, "error": "project_context_required"}, 2
        if bl is None:
            return {"ok": False, "error": "project_connection_storage_unavailable"}, 2
        data_payload = _as_dict(action.get("data")) or dict(input_payload)
        wa_id = normalize_wa_id(
            target.get("wa_id") or input_payload.get("wa_id") or data_payload.get("wa_id")
        )
        if not wa_id:
            return {"ok": False, "error": "sender_not_resolved"}, 2
        contacts = _contact_items_from_config(config)
        existing = next((c for c in contacts if c["wa_id"] == wa_id), None)
        if existing:
            return {
                "ok": False,
                "error": "contact_already_exists",
                "message": f"This number is already saved as contact '{existing['channel_id']}'.",
                "result": {"existing_channel_id": existing["channel_id"]},
            }, 0
        title = _as_str(
            input_payload.get("display_name")
            or target.get("profile_name")
            or data_payload.get("profile_name")
            or wa_id
        )
        requested_id = _as_str(input_payload.get("channel_id"))
        contact_id = requested_id or _slugify_contact_id(title)
        existing_ids = {c["channel_id"] for c in contacts}
        if requested_id and contact_id in existing_ids:
            return {"ok": False, "error": "contact_id_duplicate"}, 2
        base, suffix = contact_id, 2
        while contact_id in existing_ids:
            contact_id = f"{base}_{suffix}"
            suffix += 1
        new_contact = {
            "channel_id": contact_id,
            "display_name": title,
            "wa_id": wa_id,
            "enabled": True,
            # A new contact may be written to; it never starts workflows until switched on.
            "capabilities": {"notify": True, "workflow_trigger": False},
        }
        next_contacts = [*contacts, new_contact]
        if not _save_connection(
            bl,
            str(project_id),
            connection_config=_replace_contacts_in_config(config, next_contacts),
        ):
            return {"ok": False, "error": "project_connection_storage_unavailable"}, 2
        return {
            "ok": True,
            "message": f"Saved sender as contact '{contact_id}'.",
            "reload": ["discovered_senders_grid", "contacts_form", "health_panel"],
            "values": {"contacts": _contact_items_for_ui(next_contacts)},
        }, 0

    if action_id == "get_webhook_status":
        # WhatsApp webhooks are registered in the Meta dashboard; this only reports what
        # Flow Steward knows (registration and the last inbound event). No network call.
        registration = _callback_registration_for_project(bl, str(project_id)) if project_id else {}
        state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
        return {
            "ok": True,
            "result": {
                "provider": "whatsapp",
                "registered": bool(registration),
                "last_inbound_at": _as_str(state.get("last_inbound_at")),
                "url_configured": bool(registration),
            },
            "reload": ["whatsapp_webhook_setup", "health_panel"],
        }, 0

    if action_id == "toggle_route":
        selected_ids = [
            str(item or "").strip()
            for item in (target.get("item_ids") or [])
            if str(item or "").strip()
        ]
        if not selected_ids:
            return {"ok": False, "error": "toggle_route_requires_target_item_ids"}, 2
        if not project_id:
            return {
                "ok": True,
                "message": f"Toggled {len(selected_ids)} route(s) (standalone mode)",
                "reload": ["subscription_routes"],
            }, 0
        if (
            bl is None
            or not hasattr(bl, "list_notification_routes")
            or not hasattr(bl, "save_notification_routes")
        ):
            return {"ok": False, "error": "notification_route_storage_unavailable"}, 2
        selected_set = {item.lower() for item in selected_ids}
        routes = bl.list_notification_routes(project_id=str(project_id), enabled_only=False) or []
        prepared: list[dict] = []
        toggled = 0
        for row in routes:
            item = _as_dict(row)
            route_key = str(item.get("route_key") or "").strip()
            route_id = str(item.get("route_id") or "").strip()
            if route_key.lower() in selected_set or route_id.lower() in selected_set:
                item["enabled"] = not bool(item.get("enabled", True))
                toggled += 1
            prepared.append(
                {
                    "route_key": route_key,
                    "display_name": str(item.get("display_name") or "").strip(),
                    "workflow_id": str(item.get("workflow_id") or "").strip() or None,
                    "provider_key": str(item.get("provider_key") or "").strip(),
                    "event_type": str(item.get("event_type") or "").strip(),
                    "severity": str(item.get("severity") or "any").strip().lower() or "any",
                    "route_config": _as_dict(item.get("route_config")),
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        if toggled <= 0:
            return {"ok": False, "error": "no_matching_routes_found"}, 2
        bl.save_notification_routes(str(project_id), routes=prepared)
        return {
            "ok": True,
            "message": f"Toggled {toggled} route(s)",
            "reload": ["subscription_routes", "delivery_grid", "health_panel"],
        }, 0

    if action_id == "ingress_event":
        return _handle_ingress_event_payload(action)

    return {"ok": False, "error": f"unknown_action:{action_id}"}, 2
