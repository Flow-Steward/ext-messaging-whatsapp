#!/usr/bin/env python3
"""WhatsApp notification provider extension entrypoint.

Subprocess contract:
  - Reads JSON from stdin
  - Writes JSON to stdout
  - Exit 0 = success, 2 = bad request, 1 = internal error
"""

from __future__ import annotations

import json
import re
import sys

from flowsteward_extension_sdk import runtime_context_from_payload

import main_state as _state
from main_actions import _handle_action
from main_delivery import _deliver
from main_ingress import _handle_ingress_event_payload
from main_queries import _handle_query
from main_state import _as_dict, _as_list, _as_str

_ACTION_ERROR_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_ACTION_ERROR_MESSAGES = {
    "access_token_required": "Save an access token before running this WhatsApp action.",  # nosec B105 - an error message, not a secret
    "access_token_rejected": "WhatsApp rejected the saved access token. Replace it and try again.",  # nosec B105 - an error message, not a secret
    "phone_number_id_required": "Save the phone number ID before running this WhatsApp action.",
    "contact_already_exists": "This phone number is already saved as a contact.",
    "contact_id_duplicate": "Choose a unique contact ID.",
    "contact_required": "Choose an enabled WhatsApp contact before sending a test.",
    "delivery_not_found": "The selected WhatsApp delivery could not be found.",
    "delivery_not_retryable": "Only failed or skipped WhatsApp deliveries can be retried.",
    "delivery_retry_unavailable": "WhatsApp delivery retry is unavailable right now.",
    "invalid_payload": "The WhatsApp action input is invalid.",
    "no_matching_routes_found": "No matching notification routes were found.",
    "notification_route_storage_unavailable": "Notification route storage is unavailable right now.",
    "project_connection_required": "Select a WhatsApp connection before running this action.",
    "project_connection_storage_unavailable": "WhatsApp project storage is unavailable right now.",
    "project_context_required": "Select a project before running this WhatsApp action.",
    "sender_not_resolved": "The selected sender has no valid phone number.",
    "toggle_route_requires_target_item_ids": "Select at least one notification route.",
    "unauthorized_whatsapp_ingress": "WhatsApp could not authenticate this inbound request.",
    "whatsapp_send_ambiguous": "WhatsApp may have accepted the message; check delivery history before retrying.",
    "whatsapp_unreachable": "WhatsApp could not be reached. Try again.",
}


def _public_response(value: dict) -> dict:
    """Return one canonical public envelope from every WhatsApp boundary."""
    result = dict(value)
    if result.get("ok") is not False:
        return result
    nested = _as_dict(result.get("result"))
    declared_errors = _as_list(result.get("errors"))
    raw_error = _as_str(result.get("error"))
    candidates = [
        result.get("error_code"),
        nested.get("error_code"),
        _as_dict(declared_errors[0]).get("code") if declared_errors else "",
        raw_error,
    ]
    error_code = next(
        (
            candidate
            for raw_candidate in candidates
            if (candidate := _as_str(raw_candidate)) and _ACTION_ERROR_CODE_RE.fullmatch(candidate)
        ),
        "whatsapp_action_failed",
    )
    existing_message = _as_str(result.get("message"))
    if existing_message:
        user_message = existing_message
    elif raw_error and raw_error != error_code:
        user_message = raw_error
    elif error_code.startswith("unknown_action:"):
        user_message = "This WhatsApp action is not supported."
    else:
        user_message = _ACTION_ERROR_MESSAGES.get(
            error_code,
            "WhatsApp could not complete this action. Review the technical details and try again.",
        )
    result["error_code"] = error_code
    result["error"] = user_message
    remaining_errors = [dict(item) for item in declared_errors[1:] if isinstance(item, dict)]
    result["errors"] = [{"code": error_code, "message": user_message}, *remaining_errors]
    return result


def _with_mutations(result: dict) -> dict:
    if _state._REQUEST_HOST_MUTATIONS:
        return {**result, "host_mutations": list(_state._REQUEST_HOST_MUTATIONS)}
    return result


def main() -> int:
    raw = sys.stdin.read().strip()
    try:
        request = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                _public_response(
                    {
                        "ok": False,
                        "error_code": "invalid_json",
                        "error": f"Malformed JSON input: {exc.msg}",
                    }
                )
            )
        )
        return 2
    if not isinstance(request, dict):
        print(
            json.dumps(
                _public_response(
                    {
                        "ok": False,
                        "error_code": "invalid_payload",
                        "error": "Input payload must be a JSON object.",
                    }
                )
            )
        )
        return 2
    # The host envelope is checked before anything is read, sent or saved:
    # another contract, a missing execution-scope key or two owners is refused.
    try:
        runtime_context_from_payload(request)
    except ValueError as exc:
        refused: dict = {
            "ok": False,
            "error_code": "invalid_host_envelope",
            "error": str(exc),
            "definitely_no_external_effect": True,
        }
        if _as_str(request.get("action")) == "deliver_notification":
            refused["delivered"] = False
        print(json.dumps(_public_response(refused)))
        return 2
    _state._REQUEST_RUNTIME_CONTEXT.clear()
    _state._REQUEST_RUNTIME_CONTEXT.update(_as_dict(request.get("runtime_context")))
    _state._REQUEST_HOST_MUTATIONS.clear()

    mode = str(request.get("mode") or "").strip().lower()

    if mode == "query":
        result, _code = _handle_query(_as_dict(request.get("query")))
        result = _public_response(result)
        print(json.dumps(result))
        return 0 if result.get("ok") is True else 2

    if mode == "action":
        action = _as_dict(request.get("action"))
        request_action = _as_dict(_as_dict(request.get("request")).get("action"))
        if request_action:
            action = {**request_action, **action}
        # Artifact grants travel beside the action, not inside it; the file steps
        # read them from the action they are given.
        if "artifacts" in request and "artifacts" not in action:
            action = {**action, "artifacts": request["artifacts"]}
        result, _code = _handle_action(action)
        result = _with_mutations(_public_response(result))
        print(json.dumps(result))
        return 0 if result.get("ok") is True else 2

    request_kind = _as_str(request.get("action"))
    if request_kind == "deliver_notification":
        event = _as_dict(request.get("event"))
        route = _as_dict(request.get("route"))
        provider = _as_dict(request.get("provider"))
        target = _as_dict(_as_dict(request.get("action_payload")).get("target"))
        connection = _as_dict(target.get("connection"))
        if target:
            _state._REQUEST_RUNTIME_CONTEXT["target"] = target
        if connection:
            provider_config = {
                **_as_dict(connection.get("config") or connection.get("connection_config")),
                **_as_dict(provider.get("provider_config")),
            }
            for secret_name in ("access_token", "app_secret", "token"):
                provider_config.pop(secret_name, None)
            provider = {
                **provider,
                "provider_config": provider_config,
                "provider_secrets": _as_dict(connection.get("secrets")),
            }
        result = _public_response(_deliver(event, route, provider))
        print(json.dumps(_with_mutations(result)))
        return 0 if bool(result.get("ok")) else 2

    if request_kind == "ingress_event":
        result, _code = _handle_ingress_event_payload({"request": _as_dict(request.get("request"))})
        result = _with_mutations(_public_response(result))
        print(json.dumps(result))
        return 0 if result.get("ok") is True else 2

    error = f"Unsupported WhatsApp request: {mode or request_kind or 'missing mode'}"
    print(
        json.dumps(
            _public_response({"ok": False, "error_code": "unsupported_request", "error": error})
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
