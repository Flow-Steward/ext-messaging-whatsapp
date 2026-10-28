"""Shared helpers, the runtime payload adapter and the contact / sender state."""

from __future__ import annotations

import copy
import re
from datetime import UTC, date, datetime
from typing import Any

from flowsteward_extension_sdk import RuntimeResources, host_mutation

APPLICATION_JSON_VALUE = "application/json"
PROVIDER_KEY = "flowsteward.whatsapp"
CONNECTION_TYPE = "whatsapp_cloud"
DEFAULT_GRAPH_API_VERSION = "v26.0"
SECRET_ACCESS_TOKEN = "access_token"  # nosec B105 - the name of a stored secret, not a value
SECRET_APP_SECRET = "app_secret"  # nosec B105 - the name of a stored secret, not a value
# A phone number as WhatsApp writes it (wa_id): digits only, at most 15 (E.164).
_WA_ID_MIN_DIGITS = 7
_WA_ID_MAX_DIGITS = 15
_VERSION_RE = re.compile(r"^v\d+\.\d+$")
_DISCOVERED_LIMIT = 200

_REQUEST_RUNTIME_CONTEXT: dict[str, Any] = {}
_REQUEST_HOST_MUTATIONS: list[dict[str, Any]] = []


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _secret_refs_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        payload = copy.deepcopy(value)
        refs = value.get("refs")
        if isinstance(refs, dict):
            payload["refs"] = {
                _as_str(key): _as_str(ref)
                for key, ref in refs.items()
                if _as_str(key) and _as_str(ref)
            }
        return payload
    if isinstance(value, list):
        refs = {_as_str(item): _as_str(item) for item in value if _as_str(item)}
        return {"refs": refs} if refs else {}
    return {}


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _humanize_token(value: Any) -> str:
    text = _as_str(value)
    if not text:
        return ""
    return " ".join(part.capitalize() for part in text.replace("-", " ").replace("_", " ").split())


def _event_label(value: Any) -> str:
    text = _as_str(value)
    if not text:
        return ""
    event_map = {
        "workflow.automation.notify": "Workflow notify",
        "approval.requested": "Approval requested",
        "job.failed": "Job failed",
        "job.completed": "Job completed",
        "test.notification": "Test delivery",
    }
    return event_map.get(text, _humanize_token(text.replace(".", " ")))


def _coerce_project_id(*values: Any) -> str | None:
    for value in values:
        if value in (None, "") or isinstance(value, (dict, list, tuple, set)):
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    return None


def _apply_grid_params(items: list[dict], params: dict | None) -> tuple[list[dict], int, int, int]:
    payload = _as_dict(params)
    try:
        page = max(1, int(payload.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = max(1, min(200, int(payload.get("page_size") or payload.get("limit") or 50)))
    except (TypeError, ValueError):
        page_size = 50
    start = (page - 1) * page_size
    return items[start : start + page_size], page, page_size, len(items)


# --- phone numbers, version, contacts -----------------------------------------


def normalize_wa_id(value: Any) -> str:
    """A phone number reduced to digits (``+370 600-00000`` -> ``37060000000``), or ``""``.

    Returns ``""`` when the result is not 7 to 15 digits, so a bad number is never saved.
    """
    digits = re.sub(r"[\s+()\-.]", "", _as_str(value))
    if not digits.isdigit():
        return ""
    return digits if _WA_ID_MIN_DIGITS <= len(digits) <= _WA_ID_MAX_DIGITS else ""


def normalize_graph_version(value: Any) -> str:
    text = _as_str(value)
    return text if _VERSION_RE.fullmatch(text) else DEFAULT_GRAPH_API_VERSION


def connection_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The non-secret connection settings, normalized."""
    payload = _as_dict(config)
    return {
        "phone_number_id": _as_str(payload.get("phone_number_id")),
        "waba_id": _as_str(payload.get("waba_id")),
        "graph_api_version": normalize_graph_version(payload.get("graph_api_version")),
    }


def _contact_items_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for raw_item in _as_list(_as_dict(config).get("contacts")):
        item = _as_dict(raw_item)
        channel_id = _as_str(item.get("channel_id"))
        if not channel_id:
            continue
        capabilities = _as_dict(item.get("capabilities"))
        contacts.append(
            {
                "channel_id": channel_id,
                "display_name": _as_str(item.get("display_name")) or channel_id,
                "wa_id": normalize_wa_id(item.get("wa_id")),
                "enabled": _coerce_bool(item.get("enabled"), True),
                "capabilities": {
                    "notify": _coerce_bool(capabilities.get("notify"), False),
                    # Closed by default: only a contact the owner switched on may
                    # start workflows with its incoming messages.
                    "workflow_trigger": _coerce_bool(capabilities.get("workflow_trigger"), False),
                },
            }
        )
    return contacts


def _contact_items_for_ui(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            **dict(contact),
            "_meta": {
                "existing": True,
                "immutable_fields": ["channel_id"],
                "original_channel_id": _as_str(contact.get("channel_id")),
            },
        }
        for contact in contacts
    ]


def _replace_contacts_in_config(
    config: dict[str, Any], contacts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {**_as_dict(config), "contacts": contacts}


def _slugify_contact_id(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", _as_str(value).lower()).strip("_")
    return text[:48] or "whatsapp_contact"


# --- discovered senders (fed by inbound messages) --------------------------------


def _discovered_sender_row(
    *, wa_id: str, profile_name: str, last_seen_at: str = "", last_message_type: str = ""
) -> dict[str, Any]:
    return {
        "wa_id": _as_str(wa_id),
        "profile_name": _as_str(profile_name),
        "last_message_type": _as_str(last_message_type),
        "last_seen_at": _as_str(last_seen_at) or datetime.now(UTC).isoformat(),
    }


def _upsert_discovered_sender(
    discovered: list[dict[str, Any]], row: dict[str, Any]
) -> list[dict[str, Any]]:
    wa_id = _as_str(_as_dict(row).get("wa_id"))
    if not wa_id:
        return discovered
    rows = [_as_dict(item) for item in discovered if _as_str(_as_dict(item).get("wa_id")) != wa_id]
    previous = next(
        (_as_dict(i) for i in discovered if _as_str(_as_dict(i).get("wa_id")) == wa_id), {}
    )
    merged = {**previous, **{k: v for k, v in row.items() if v not in (None, "")}}
    rows.append(merged)
    rows.sort(key=lambda item: _as_str(item.get("last_seen_at")), reverse=True)
    return rows[:_DISCOVERED_LIMIT]


def _annotate_discovered_senders(
    discovered: list[dict[str, Any]], contacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_wa_id = {
        _as_str(_as_dict(c).get("wa_id")): _as_dict(c)
        for c in contacts
        if _as_str(_as_dict(c).get("wa_id"))
    }
    annotated: list[dict[str, Any]] = []
    for row in discovered:
        payload = dict(_as_dict(row))
        existing = by_wa_id.get(_as_str(payload.get("wa_id")))
        payload["existing_channel_id"] = _as_str(existing.get("channel_id")) if existing else ""
        payload["already_saved"] = bool(existing)
        payload["suggested_channel_id"] = _slugify_contact_id(
            payload.get("profile_name") or payload.get("wa_id")
        )
        annotated.append(payload)
    return annotated


def _normalize_inbound_state(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = _as_dict(payload)
    discovered: list[dict[str, Any]] = []
    for row in _as_list(source.get("discovered_senders")):
        item = _as_dict(row)
        wa_id = _as_str(item.get("wa_id"))
        if wa_id:
            discovered = _upsert_discovered_sender(
                discovered,
                _discovered_sender_row(
                    wa_id=wa_id,
                    profile_name=_as_str(item.get("profile_name")),
                    last_seen_at=_as_str(item.get("last_seen_at")),
                    last_message_type=_as_str(item.get("last_message_type")),
                ),
            )
    return {
        "discovered_senders": discovered,
        "last_inbound_at": _as_str(source.get("last_inbound_at")),
        "seen_message_keys": [
            _as_str(key) for key in _as_list(source.get("seen_message_keys")) if _as_str(key)
        ][-_SEEN_KEYS_LIMIT:],
    }


# Dedupe memory: Meta retries a failed delivery for days, so recent keys are kept.
_SEEN_KEYS_LIMIT = 500


# --- runtime payload adapter ------------------------------------------------------


class RuntimePayloadStore:
    """Adapter over host-provided runtime payload data, not host services."""

    def __init__(self, runtime_context: dict[str, Any] | None = None):
        self.context = _as_dict(runtime_context)
        self.runtime_resources = RuntimeResources.from_context(self.context)
        self.resources = self.runtime_resources.values
        self.host_mutations: list[dict[str, Any]] = []

    def _connection_payload(self) -> dict[str, Any]:
        for key in ("connection", "project_connection"):
            payload = _as_dict(self.context.get(key))
            if payload:
                return dict(payload)
        payload = _as_dict(self.resources.get("project_connection"))
        if payload:
            return dict(payload)
        target = _as_dict(self.context.get("target"))
        payload = _as_dict(target.get("connection"))
        return dict(payload) if payload else {}

    def get_project_connection(
        self, project_id: str, extension_id: str, enabled_only: bool = False
    ) -> dict[str, Any]:
        row = self._connection_payload()
        if not row:
            return {}
        if _as_str(row.get("extension_id") or extension_id) != extension_id:
            return {}
        if enabled_only and row.get("enabled") is False:
            return {}
        row.setdefault("project_id", project_id)
        row.setdefault("extension_id", extension_id)
        if "connection_config" not in row and isinstance(row.get("config"), dict):
            row["connection_config"] = dict(row["config"])
        if "secret_refs" not in row and isinstance(row.get("secrets"), dict):
            row["secret_refs"] = {"refs": {key: key for key in row["secrets"]}}
        return row

    def upsert_project_connection(self, **payload: Any) -> None:
        update = dict(payload)
        self.resources["project_connection"] = update
        self.host_mutations.append(host_mutation("project_connection_upsert", update))
        _REQUEST_HOST_MUTATIONS.append(self.host_mutations[-1])

    def get_provider_secret(self, **payload: Any) -> str:
        secret_key = _as_str(payload.get("secret_key"))
        return _as_str(_as_dict(self.resources.get("provider_secrets")).get(secret_key))

    def list_notification_delivery_logs(self, **payload: Any) -> list[dict[str, Any]]:
        _ = payload
        return [
            dict(row)
            for row in _as_list(self.resources.get("notification_deliveries"))
            if isinstance(row, dict)
        ]

    def log_notification_delivery(self, **payload: Any) -> dict[str, Any]:
        mutation = host_mutation("notification_delivery_log", payload)
        self.host_mutations.append(mutation)
        _REQUEST_HOST_MUTATIONS.append(mutation)
        return dict(payload)

    def list_notification_routes(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        _ = (args, kwargs)
        return [
            dict(row)
            for row in _as_list(self.resources.get("notification_routes"))
            if isinstance(row, dict)
        ]

    def save_notification_routes(self, project_id: str, routes: list[dict] | None = None) -> None:
        _ = project_id
        normalized = [dict(row) for row in routes or [] if isinstance(row, dict)]
        self.resources["notification_routes"] = normalized
        mutation = host_mutation("notification_routes_save", {"routes": normalized})
        self.host_mutations.append(mutation)
        _REQUEST_HOST_MUTATIONS.append(mutation)

    def list_extension_callback_registrations(self, **payload: Any) -> list[dict[str, Any]]:
        _ = payload
        return [
            dict(row)
            for row in _as_list(self.resources.get("callback_registrations"))
            if isinstance(row, dict)
        ]


def _runtime_logger(runtime_context: dict[str, Any] | None = None):
    context = _as_dict(runtime_context)
    if any(key in context for key in ("connection", "project_connection", "target", "resources")):
        return RuntimePayloadStore(context)
    return None


def _connection_row_for_project(bl, project_id: str) -> dict[str, Any]:
    if bl is None or not hasattr(bl, "get_project_connection"):
        return {}
    try:
        row = bl.get_project_connection(
            project_id=str(project_id), extension_id=PROVIDER_KEY, enabled_only=False
        )
    except Exception:
        return {}
    return dict(row) if isinstance(row, dict) else {}


def _save_connection(
    bl,
    project_id: str,
    *,
    connection_config: dict[str, Any] | None = None,
    ingress_config: dict[str, Any] | None = None,
) -> bool:
    """Store config / ingress state through a ``project_connection_upsert`` host mutation."""
    if bl is None or not hasattr(bl, "upsert_project_connection"):
        return False
    existing = _connection_row_for_project(bl, project_id)
    try:
        bl.upsert_project_connection(
            connection_id=_as_str(existing.get("connection_id")),
            project_id=str(project_id),
            extension_id=PROVIDER_KEY,
            account_id=_as_str(existing.get("account_id"))
            or _as_str(_REQUEST_RUNTIME_CONTEXT.get("account_id"))
            or None,
            connection_kind=str(existing.get("connection_kind") or "extension").strip()
            or "extension",
            connection_type=_as_str(existing.get("connection_type")) or CONNECTION_TYPE,
            extension_install_id=_as_str(existing.get("extension_install_id"))
            or _as_str(_REQUEST_RUNTIME_CONTEXT.get("extension_install_id")),
            display_name=_as_str(existing.get("display_name")),
            enabled=_coerce_bool(existing.get("enabled"), True),
            connection_config=dict(
                connection_config
                if connection_config is not None
                else _as_dict(existing.get("connection_config"))
            ),
            secret_refs=_secret_refs_payload(existing.get("secret_refs")),
            connection_routes=list(existing.get("connection_routes") or []),
            ingress_config=dict(
                ingress_config
                if ingress_config is not None
                else _as_dict(existing.get("ingress_config"))
            ),
            workflow_mappings=_as_dict(existing.get("workflow_mappings")),
        )
        return True
    except Exception:
        return False


def _callback_registration_for_project(bl: Any, project_id: str) -> dict[str, Any]:
    if bl is None or not hasattr(bl, "list_extension_callback_registrations"):
        return {}
    try:
        rows = bl.list_extension_callback_registrations(
            extension_id=PROVIDER_KEY, callback_id="updates", project_id=str(project_id)
        )
    except Exception:
        rows = []
    return _as_dict(rows[0]) if rows else {}
