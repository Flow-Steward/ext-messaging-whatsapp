from __future__ import annotations

from typing import Any

from main_readiness import derive_readiness
from main_state import (
    _REQUEST_RUNTIME_CONTEXT,
    PROVIDER_KEY,
    SECRET_ACCESS_TOKEN,
    SECRET_APP_SECRET,
    _annotate_discovered_senders,
    _apply_grid_params,
    _as_dict,
    _as_list,
    _as_str,
    _callback_registration_for_project,
    _connection_row_for_project,
    _contact_items_for_ui,
    _contact_items_from_config,
    _coerce_project_id,
    _event_label,
    _humanize_token,
    _json_safe_value,
    _normalize_inbound_state,
    _runtime_logger,
    connection_settings,
)
from main_transport import get_phone_number


def _secret(bl: Any, project_id: Any, key: str) -> str:
    if bl is None or not project_id or not hasattr(bl, "get_provider_secret"):
        return ""
    try:
        return _as_str(
            bl.get_provider_secret(
                provider_key=PROVIDER_KEY,
                secret_key=key,
                project_id=str(project_id),
                include_global=False,
            )
        )
    except Exception:
        return ""


def _provider_deliveries(bl: Any, project_id: Any, limit: int) -> list[dict[str, Any]]:
    if bl is None or not project_id or not hasattr(bl, "list_notification_delivery_logs"):
        return []
    try:
        rows = bl.list_notification_delivery_logs(project_id=str(project_id), limit=limit) or []
    except Exception:
        return []
    return [
        _as_dict(row)
        for row in rows
        if _as_str(_as_dict(row).get("provider_key")).lower() == PROVIDER_KEY
    ]


def _need_project() -> tuple[dict, int]:
    return {"ok": False, "error": "project_context_required"}, 2


def _handle_query(query: dict) -> tuple[dict, int]:
    query_id = str(query.get("query_id") or "").strip()
    context = _as_dict(query.get("context"))
    params = _as_dict(query.get("params"))
    project_id = _coerce_project_id(context.get("project_id"), params.get("project_id"))
    runtime_context = _as_dict(query.get("runtime_context")) or _REQUEST_RUNTIME_CONTEXT
    bl = _runtime_logger(runtime_context)
    connection = (
        _connection_row_for_project(bl, str(project_id)) if (bl is not None and project_id) else {}
    )
    config = _as_dict(connection.get("connection_config"))

    if query_id == "provider_health":
        settings = connection_settings(config)
        token = _secret(bl, project_id, SECRET_ACCESS_TOKEN)
        identity = (
            get_phone_number(token, settings) if token and settings["phone_number_id"] else None
        )
        readiness = derive_readiness(
            connection=connection,
            token_present=bool(token),
            app_secret_present=bool(_secret(bl, project_id, SECRET_APP_SECRET)),
            phone_number_id=settings["phone_number_id"],
            identity_response=identity,
            contacts=_contact_items_from_config(config),
            registration=_callback_registration_for_project(bl, str(project_id))
            if project_id
            else {},
            inbound_state=_normalize_inbound_state(_as_dict(connection.get("ingress_config"))),
            deliveries=_provider_deliveries(bl, project_id, 50),
        )
        return {
            "ok": True,
            "checks": readiness["checks"],
            "items": [{"status": readiness["summary"]["readiness"]}],
            "summary": {
                **readiness["summary"],
                "project_id": project_id,
                "runtime_available": True,
            },
        }, 0

    if query_id == "delivery_summary":
        deliveries = _provider_deliveries(bl, project_id, 2000)
        delivered = sum(1 for r in deliveries if _as_str(r.get("status")).lower() == "delivered")
        failed = sum(1 for r in deliveries if _as_str(r.get("status")).lower() == "failed")
        return {
            "ok": True,
            "items": [
                {"label": "Total", "value": len(deliveries)},
                {"label": "Delivered", "value": delivered},
                {"label": "Failed", "value": failed},
            ],
            "summary": {
                "provider": "whatsapp",
                "total": len(deliveries),
                "delivered": delivered,
                "failed": failed,
            },
        }, 0

    if query_id == "list_deliveries":
        rows = []
        for payload in _provider_deliveries(bl, project_id, 2000):
            rows.append(
                {
                    "delivery_id": payload.get("delivery_id"),
                    "event_type": _as_str(payload.get("event_type")),
                    "event_label": _event_label(payload.get("event_type")),
                    "severity": _as_str(payload.get("severity") or "info").lower() or "info",
                    "status": _as_str(payload.get("status")).lower() or "unknown",
                    "delivered_at": _json_safe_value(payload.get("created_at")),
                    "error_text": _as_str(payload.get("error_text")),
                    "workflow_id": _as_str(payload.get("workflow_id")),
                    "workflow_label": _humanize_token(payload.get("workflow_id")),
                    "job_id": payload.get("job_id"),
                    "provider_message_id": _as_str(payload.get("provider_message_id")),
                    "route_id": payload.get("route_id"),
                }
            )
        effective_params = dict(params)
        effective_params.setdefault("page_size", 20)
        paged, page, page_size, total = _apply_grid_params(rows, effective_params)
        return {
            "ok": True,
            "items": paged,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": (page * page_size) < total,
            "summary": {"provider": "whatsapp", "project_id": project_id, "total": total},
        }, 0

    if query_id == "list_routes":
        routes: list[dict[str, Any]] = []
        if bl is not None and project_id and hasattr(bl, "list_notification_routes"):
            for row in (
                bl.list_notification_routes(project_id=str(project_id), enabled_only=False) or []
            ):
                payload = _as_dict(row)
                if _as_str(payload.get("provider_key")).lower() != PROVIDER_KEY:
                    continue
                routes.append(
                    {
                        "route_id": payload.get("route_id"),
                        "route_key": _as_str(payload.get("route_key")),
                        "event_type": _as_str(payload.get("event_type")),
                        "severity": _as_str(payload.get("severity")).lower() or "any",
                        "workflow_id": _as_str(payload.get("workflow_id")),
                        "channel_id": _as_str(
                            _as_dict(payload.get("route_config")).get("channel_id")
                        ),
                        "enabled": bool(payload.get("enabled", True)),
                    }
                )
        paged, page, page_size, total = _apply_grid_params(routes, params)
        return {
            "ok": True,
            "items": paged,
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": (page * page_size) < total,
            "summary": {"provider": "whatsapp", "project_id": project_id, "total": total},
        }, 0

    if query_id == "get_config":
        if not project_id:
            return _need_project()
        return {
            "ok": True,
            "values": connection_settings(config),
            "summary": {"provider": "whatsapp", "project_id": project_id},
        }, 0

    if query_id == "get_secrets":
        if not project_id:
            return _need_project()
        return {
            "ok": True,
            # A secret is never echoed back to the form.
            "values": {SECRET_ACCESS_TOKEN: "", SECRET_APP_SECRET: ""},
            "summary": {
                "provider": "whatsapp",
                "project_id": project_id,
                "access_token_configured": bool(_secret(bl, project_id, SECRET_ACCESS_TOKEN)),
                "app_secret_configured": bool(_secret(bl, project_id, SECRET_APP_SECRET)),
            },
        }, 0

    if query_id == "list_contacts":
        if not project_id:
            return _need_project()
        contacts = _contact_items_for_ui(_contact_items_from_config(config))
        paged, page, page_size, total = _apply_grid_params(contacts, params)
        return {
            "ok": True,
            "items": paged,
            "page": page,
            "page_size": page_size,
            "total": total,
            "values": {"contacts": contacts},
            "summary": {"provider": "whatsapp", "project_id": project_id, "total": total},
        }, 0

    if query_id == "get_inbound_status":
        if not project_id:
            return _need_project()
        state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
        registration = _callback_registration_for_project(bl, str(project_id))
        item = {
            "registered": "yes" if registration else "no",
            "callback_registration_id": _as_str(registration.get("id")),
            "last_inbound_at": _as_str(state.get("last_inbound_at")),
            "discovered_senders": len(_as_list(state.get("discovered_senders"))),
            "app_secret_saved": "yes" if _secret(bl, project_id, SECRET_APP_SECRET) else "no",
        }
        return {
            "ok": True,
            "items": [item],
            "summary": {"provider": "whatsapp", "project_id": project_id, **item},
        }, 0

    if query_id == "list_discovered_senders":
        if not project_id:
            return _need_project()
        state = _normalize_inbound_state(_as_dict(connection.get("ingress_config")))
        discovered = _annotate_discovered_senders(
            list(_as_list(state.get("discovered_senders"))), _contact_items_from_config(config)
        )
        paged, page, page_size, total = _apply_grid_params(discovered, params)
        return {
            "ok": True,
            "items": paged,
            "page": page,
            "page_size": page_size,
            "total": total,
            "values": {"discovered_senders": discovered},
            "summary": {"provider": "whatsapp", "project_id": project_id, "total": total},
        }, 0

    return {"ok": False, "error": f"unknown_query:{query_id}"}, 2
