"""Overview readiness, derived from current facts.

Four sections (Connection, Contacts, Inbound, Deliveries), each with a readiness word from a
closed vocabulary, a human detail and, when something is incomplete, one contextual action
pointing at the page and component that fixes it.

``setup_incomplete`` means required configuration is missing; ``ready`` means the access token
works and an enabled notification contact exists; ``needs_attention`` means a configured part
is degraded; ``failed`` is a confirmed failure. Inbound is optional: ``off`` never reduces
outbound readiness. Nothing here touches the network; the caller supplies the identity answer.
"""

from __future__ import annotations

from typing import Any

READINESS_LABELS: dict[str, str] = {
    "ready": "Ready",
    "setup_incomplete": "Setup incomplete",
    "needs_attention": "Needs attention",
    "failed": "Failed",
    "off": "Off",
}

_PLATFORM_STATUS: dict[str, str] = {
    "ready": "healthy",
    "off": "healthy",
    "setup_incomplete": "warning",
    "needs_attention": "warning",
    "failed": "failed",
}

MESSAGES: dict[str, str] = {
    "access_token_required": "Save an access token on the Setup page before verifying.",  # nosec B105 - an error message, not a secret
    "phone_number_id_required": "Save the phone number ID on the Setup page before verifying.",
    "access_token_rejected": (  # nosec B105 - an error message, not a secret
        "WhatsApp rejected this access token or phone number ID. Create a new system user "
        "token in Meta Business Settings and try again."
    ),
    "whatsapp_unreachable": (
        "The settings were saved, but WhatsApp could not be reached for verification. "
        "Try Verify again."
    ),
    "contact_required": "Choose an enabled WhatsApp contact before sending a test notification.",
    "storage_unavailable": "WhatsApp settings could not be saved. No partial setup was applied.",
}

_INBOUND_OPTIONAL = (
    "needed only to start workflows from incoming messages and to see delivery status"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def classify_api_failure(response: dict[str, Any] | None) -> str:
    """``unauthorized`` when WhatsApp rejected the token or number, otherwise ``unreachable``."""
    payload = _as_dict(response)
    if payload.get("error_code") in {"whatsapp_token_invalid", "whatsapp_parameter_invalid"}:
        return "unauthorized"
    if payload.get("graph_error_code") in {190, 100}:
        return "unauthorized"
    return "unreachable"


def _check(
    name: str, readiness: str, detail: str, *, action: tuple[str, str, str] | None = None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "status": _PLATFORM_STATUS[readiness],
        "readiness": readiness,
        "label": READINESS_LABELS[readiness],
        "detail": detail,
    }
    if action is not None:
        label, page_id, component_id = action
        row["action"] = {"label": label, "page_id": page_id, "component_id": component_id}
    return row


def _connection_check(
    *, token_present: bool, phone_number_id: str, identity_response: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    card = ("setup", "secrets_form")
    if not token_present or not phone_number_id:
        missing = "access token" if not token_present else "phone number ID"
        return (
            _check(
                "Connection",
                "setup_incomplete",
                f"Setup incomplete: no {missing} saved.",
                action=("Configure connection", *card),
            ),
            "",
        )
    response = _as_dict(identity_response)
    if _truthy(response.get("ok")):
        data = _as_dict(response.get("data"))
        number = _text(data.get("display_phone_number"))
        name = _text(data.get("verified_name"))
        detail = f"Connected as {name} {number}".strip() if (name or number) else "Connected"
        return _check("Connection", "ready", detail), number
    if classify_api_failure(response) == "unauthorized":
        return (
            _check(
                "Connection",
                "failed",
                MESSAGES["access_token_rejected"],
                action=("Replace token", *card),
            ),
            "",
        )
    return (
        _check(
            "Connection",
            "needs_attention",
            MESSAGES["whatsapp_unreachable"],
            action=("Verify connection", *card),
        ),
        "",
    )


def _contacts_check(contacts: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    def valid_enabled(value: Any) -> bool:
        row = _as_dict(value)
        capabilities = _as_dict(row.get("capabilities"))
        return bool(
            _text(row.get("channel_id"))
            and _text(row.get("display_name"))
            and _text(row.get("wa_id"))
            and row.get("enabled") is True
            and isinstance(capabilities.get("notify"), bool)
        )

    enabled = [row for row in contacts if valid_enabled(row)]
    notify = sum(1 for row in enabled if _as_dict(_as_dict(row).get("capabilities")).get("notify"))
    triggers = sum(
        1
        for row in enabled
        if _truthy(_as_dict(_as_dict(row).get("capabilities")).get("workflow_trigger"))
    )
    if not notify:
        return (
            _check(
                "Contacts",
                "setup_incomplete",
                "No enabled notification contact.",
                action=("Add contact", "contacts", "contacts_form"),
            ),
            notify,
            triggers,
        )
    return (
        _check("Contacts", "ready", f"Enabled: {notify} notification, {triggers} workflow-trigger"),
        notify,
        triggers,
    )


def _inbound_check(
    *, app_secret_present: bool, registration: dict[str, Any], inbound_state: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    configure = ("Set up inbound", "setup", "whatsapp_webhook_setup")
    last = _text(inbound_state.get("last_inbound_at"))
    if last:
        return _check("Inbound", "ready", f"Webhook active; last event {last}"), "Webhook"
    if registration and app_secret_present:
        return (
            _check(
                "Inbound",
                "needs_attention",
                "Registered, but no webhook has arrived yet. Paste the callback URL and verify "
                "token into the Meta dashboard and subscribe to the messages field.",
                action=configure,
            ),
            "Waiting",
        )
    return (
        _check("Inbound", "off", f"Off (optional; {_INBOUND_OPTIONAL})", action=configure),
        "Off",
    )


def _deliveries_check(deliveries: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    rows = [_as_dict(row) for row in deliveries if isinstance(row, dict)]
    rows.sort(key=lambda row: _text(row.get("created_at") or row.get("delivered_at")), reverse=True)
    recent = rows[:20]
    delivered = sum(1 for row in recent if _text(row.get("status")).lower() == "delivered")
    failed = sum(1 for row in recent if _text(row.get("status")).lower() == "failed")
    if not recent:
        return _check("Deliveries", "ready", "No deliveries yet."), 0, 0
    last = recent[0]
    counts = f"{delivered} delivered, {failed} failed recently."
    if _text(last.get("status")).lower() == "failed":
        reason = _text(last.get("error_text")) or "no error text recorded"
        return (
            _check(
                "Deliveries",
                "failed",
                f"{counts} Last delivery failed: {reason}",
                action=("Review deliveries", "deliveries", "delivery_grid"),
            ),
            delivered,
            failed,
        )
    when = _text(last.get("created_at") or last.get("delivered_at"))
    suffix = f" at {when}" if when else ""
    return (
        _check(
            "Deliveries",
            "ready",
            f"{counts} Last: {_text(last.get('status')) or 'unknown'}{suffix}",
        ),
        delivered,
        failed,
    )


def derive_readiness(
    *,
    connection: dict[str, Any] | None,
    token_present: bool,
    app_secret_present: bool,
    phone_number_id: str,
    identity_response: dict[str, Any] | None,
    contacts: list[dict[str, Any]],
    registration: dict[str, Any] | None,
    inbound_state: dict[str, Any] | None,
    deliveries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Checks and summary for the Overview readiness panel. Never includes a secret."""
    conn, number = _connection_check(
        token_present=token_present,
        phone_number_id=phone_number_id,
        identity_response=identity_response,
    )
    contacts_row, notify_count, trigger_count = _contacts_check(list(contacts or []))
    inbound, inbound_label = _inbound_check(
        app_secret_present=app_secret_present,
        registration=_as_dict(registration),
        inbound_state=_as_dict(inbound_state),
    )
    deliveries_row, delivered, failed = _deliveries_check(list(deliveries or []))
    checks = [conn, contacts_row, inbound, deliveries_row]

    if conn["readiness"] == "failed":
        readiness = "failed"
    elif "setup_incomplete" in {conn["readiness"], contacts_row["readiness"]}:
        readiness = "setup_incomplete"
    elif any(row["readiness"] in {"needs_attention", "failed"} for row in checks):
        readiness = "needs_attention"
    else:
        readiness = "ready"

    return {
        "checks": checks,
        "summary": {
            "provider": "whatsapp",
            "readiness": readiness,
            "status": READINESS_LABELS[readiness],
            "connection_id": _text(_as_dict(connection).get("connection_id")),
            "display_phone_number": number,
            "enabled_notification_contacts": notify_count,
            "enabled_workflow_trigger_contacts": trigger_count,
            "inbound": inbound_label,
            "deliveries": {"delivered": delivered, "failed": failed},
        },
    }


__all__ = ["MESSAGES", "READINESS_LABELS", "classify_api_failure", "derive_readiness"]
