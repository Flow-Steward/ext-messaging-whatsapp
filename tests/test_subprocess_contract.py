"""The host envelope and the subprocess contract of main.py (no network is reachable here)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from subprocess_contract_helpers import GROUP_STEP_SCOPE, ROOT_STEP_SCOPE, _invoke, host_envelope

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
CONNECTION = {
    "connection_id": "c1",
    "config": {
        "phone_number_id": "1",
        "contacts": [
            {
                "channel_id": "ops",
                "display_name": "Ops",
                "wa_id": "37060000001",
                "enabled": True,
                "capabilities": {"notify": True, "workflow_trigger": True},
            }
        ],
    },
    "secrets": {"access_token": "t0k3n-secret-value"},  # pragma: allowlist secret
}


def test_health_probe_is_healthy():
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "health.py")], capture_output=True, text=True, timeout=10
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"status": "healthy", "provider": "whatsapp"}


def test_malformed_json_and_non_objects_are_bad_requests():
    for raw in ("{nope", "[]"):
        proc = subprocess.run(
            [sys.executable, str(BUNDLE_ROOT / "main.py")],
            input=raw,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 2
        assert json.loads(proc.stdout)["ok"] is False


@pytest.mark.parametrize(
    "request_payload",
    [
        {"mode": "query", "query": {"query_id": "get_config"}},
        {"mode": "action", "action": {"action_id": "send_message"}},
        {"action": "deliver_notification"},
    ],
)
def test_a_wrong_contract_version_is_refused_before_anything_runs(request_payload):
    response, code = _invoke(
        {**request_payload, "contract_version": "extension_host_v1"}, envelope=False
    )
    assert code == 2
    assert response["error_code"] == "invalid_host_envelope"
    assert response["definitely_no_external_effect"] is True


def test_a_missing_scope_key_is_refused():
    payload = {
        "mode": "query",
        "query": {"query_id": "get_config"},
        "contract_version": "extension_host_v2",
        "runtime_context": {"group_id": None},
    }
    response, code = _invoke(payload, envelope=False)
    assert (code, response["error_code"]) == (2, "invalid_host_envelope")


def test_a_delivery_refusal_says_not_delivered():
    response, code = _invoke(
        {"action": "deliver_notification", "contract_version": "x"}, envelope=False
    )
    assert code == 2
    assert response["delivered"] is False


def step_request(action_id: str, payload: dict[str, Any], *, test_mode: bool) -> dict[str, Any]:
    return {
        "mode": "action",
        "action": {
            "action_id": action_id,
            "context": {"project_id": "7", "test_mode": test_mode},
            "target": {"connection": CONNECTION},
            "input": payload,
        },
    }


@pytest.mark.parametrize("scope", [None, ROOT_STEP_SCOPE, GROUP_STEP_SCOPE])
def test_a_test_run_step_is_suppressed_in_every_scope(scope):
    request = host_envelope(step_request("send_message", {"text": "hi"}, test_mode=True), scope)
    proc = subprocess.run(
        [sys.executable, str(BUNDLE_ROOT / "main.py")],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(BUNDLE_ROOT),
    )
    assert proc.returncode == 0, proc.stdout
    response = json.loads(proc.stdout)
    assert response["external_effect_status"] == "suppressed"
    assert "t0k3n" not in proc.stdout


def test_a_step_error_is_one_public_envelope_without_the_token():
    response, code = _invoke(step_request("send_message", {"text": "x" * 4097}, test_mode=False))
    assert code == 2
    assert response["error_code"] == "message_too_long"
    assert response["errors"][0]["code"] == "message_too_long"
    assert "t0k3n" not in json.dumps(response)


def test_removed_telegram_actions_are_unknown():
    for removed in ("edit_message", "delete_message", "pin_message", "unpin_message"):
        response, code = _invoke(step_request(removed, {}, test_mode=False))
        assert code == 2
        assert response["error_code"].startswith("unknown_action")


def test_unsupported_requests_are_refused():
    response, code = _invoke({"mode": "nope"})
    assert (code, response["error_code"]) == (2, "unsupported_request")


def test_ingress_event_returns_events_for_an_allowed_sender():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "1"},
                            "contacts": [{"profile": {"name": "Ann"}, "wa_id": "37060000001"}],
                            "messages": [
                                {
                                    "from": "37060000001",
                                    "id": "wamid.X",
                                    "timestamp": "1",
                                    "type": "text",
                                    "text": {"body": "hi"},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }
    response, code = _invoke(
        {
            "action": "ingress_event",
            "request": {
                "json": payload,
                "callback": {"authenticated": True, "project_id": "7"},
                "provider": {"provider_config": CONNECTION["config"]},
            },
        }
    )
    assert code == 0, response
    assert [e["event_type"] for e in response["events"]] == ["whatsapp.message.received"]
    assert response["events"][0]["payload"]["text"] == "hi"


def test_ingress_event_refuses_an_unauthenticated_request():
    response, code = _invoke({"action": "ingress_event", "request": {"json": {}, "callback": {}}})
    assert code == 2
    assert response["error_code"] == "unauthorized_whatsapp_ingress"


def test_deliver_notification_refuses_a_missing_contact_without_the_network():
    response, code = _invoke(
        {
            "action": "deliver_notification",
            "event": {"event_type": "job.failed"},
            "route": {"route_config": {}},
            "action_payload": {"target": {"connection": CONNECTION}},
        }
    )
    assert code == 2
    assert response["error_code"] == "channel_id_required"
    assert response["delivered"] is False


def test_deliver_notification_refuses_a_contact_without_notify():
    connection = json.loads(json.dumps(CONNECTION))
    connection["config"]["contacts"][0]["capabilities"]["notify"] = False
    response, code = _invoke(
        {
            "action": "deliver_notification",
            "event": {"event_type": "job.failed"},
            "route": {"route_config": {"channel_id": "ops"}},
            "action_payload": {"target": {"connection": connection}},
        }
    )
    assert (code, response["error_code"]) == (2, "channel_missing_notify")


def test_queries_without_a_project_ask_for_one():
    response, code = _invoke({"mode": "query", "query": {"query_id": "list_contacts"}})
    assert (code, response["error_code"]) == (2, "project_context_required")
    response, code = _invoke({"mode": "query", "query": {"query_id": "nope"}})
    assert code == 2
