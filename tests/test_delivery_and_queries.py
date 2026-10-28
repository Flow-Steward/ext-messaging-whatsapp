"""Notification delivery (mocked transport), queries and the readiness panel."""

from __future__ import annotations

import json

from conftest import PHONE_NUMBER_ID, TOKEN, graph_error, ok_message

import main_actions
import main_delivery
import main_queries
import main_readiness

CONFIG = {
    "phone_number_id": PHONE_NUMBER_ID,
    "contacts": [
        {
            "channel_id": "ops",
            "display_name": "Ops",
            "wa_id": "37060000001",
            "enabled": True,
            "capabilities": {"notify": True, "workflow_trigger": False},
        }
    ],
}
PROVIDER = {"provider_config": CONFIG, "provider_secrets": {"access_token": TOKEN}}
EVENT = {"event_type": "job.failed", "severity": "error", "summary": "Job 12 failed"}
ROUTE = {"severity": "error", "route_config": {"channel_id": "ops", "message_suffix": "-- team"}}


def test_notification_is_a_plain_text_message(graph):
    graph.reply(200, ok_message("wamid.N"))
    result = main_delivery._deliver(EVENT, ROUTE, PROVIDER)
    assert result["ok"] is True
    assert result["provider_message_id"] == "wamid.N"
    body = graph.json_body()
    assert body["type"] == "text"
    assert body["to"] == "37060000001"
    assert "[ERROR] Job failed" in body["text"]["body"]
    assert "Job 12 failed" in body["text"]["body"]
    assert body["text"]["body"].endswith("-- team")


def test_notification_outside_the_window_surfaces_the_error(graph):
    graph.reply(400, graph_error(131047))
    result = main_delivery._deliver(EVENT, ROUTE, PROVIDER)
    assert result["ok"] is False
    assert result["delivered"] is False
    assert result["error_code"] == "whatsapp_window_closed"
    assert "template" in result["error"]


def test_a_long_generated_notification_is_shortened_visibly(graph):
    graph.reply(200, ok_message())
    main_delivery._deliver({**EVENT, "summary": "x" * 5000}, ROUTE, PROVIDER)
    text = graph.json_body()["text"]["body"]
    assert len(text) == 4096
    assert text.endswith("…")


def test_notification_target_errors_make_no_request(graph):
    assert (
        main_delivery._deliver(EVENT, {"route_config": {}}, PROVIDER)["error_code"]
        == "channel_id_required"
    )
    assert (
        main_delivery._deliver(EVENT, {"route_config": {"channel_id": "x"}}, PROVIDER)["error_code"]
        == "unknown_channel_id"
    )
    assert graph.requests == []


def test_test_delivery_action_sends_a_test(graph):
    graph.reply(200, ok_message())
    result, code = main_actions._handle_action(
        {
            "action_id": "test_delivery",
            "context": {"project_id": "7"},
            "input": {"channel_id": "ops"},
            "runtime_context": {
                "project_connection": {"connection_id": "c", "connection_config": CONFIG},
                "resources": {"provider_secrets": {"access_token": TOKEN}},
            },
        }
    )
    assert code == 0, result
    assert result["result"]["provider_message_id"] == "wamid.OUT1"
    assert TOKEN not in json.dumps(result)


def test_verify_connection_reads_the_phone_number(graph):
    graph.reply(200, {"verified_name": "Shop", "display_phone_number": "+370 600 00000"})
    result, code = main_actions._handle_action(
        {
            "action_id": "verify_connection",
            "context": {"project_id": "7"},
            "input": {"provider": {"access_token": TOKEN}},
            "runtime_context": {
                "project_connection": {"connection_id": "c", "connection_config": CONFIG}
            },
        }
    )
    assert code == 0, result
    assert result["result"]["verified"] is True
    assert TOKEN not in json.dumps(result)


def test_verify_connection_reports_a_rejected_token(graph):
    graph.reply(401, graph_error(190))
    result, code = main_actions._handle_action(
        {
            "action_id": "verify_connection",
            "context": {"project_id": "7"},
            "input": {"provider": {"access_token": TOKEN}},
            "runtime_context": {
                "project_connection": {"connection_id": "c", "connection_config": CONFIG}
            },
        }
    )
    assert code == 2
    assert result["error"] == "access_token_rejected"


def test_get_secrets_never_returns_a_secret():
    result, code = main_queries._handle_query(
        {
            "query_id": "get_secrets",
            "context": {"project_id": "7"},
            "runtime_context": {
                "project_connection": {"connection_id": "c", "connection_config": CONFIG},
                "resources": {
                    "provider_secrets": {"access_token": TOKEN, "app_secret": "s3cr3t-app"}
                },
            },
        }
    )
    assert code == 0
    assert result["values"] == {"access_token": "", "app_secret": ""}
    assert result["summary"]["access_token_configured"] is True
    assert result["summary"]["app_secret_configured"] is True
    assert TOKEN not in json.dumps(result)


def test_list_contacts_and_discovered_senders():
    context = {
        "project_connection": {
            "connection_id": "c",
            "connection_config": CONFIG,
            "ingress_config": {
                "discovered_senders": [
                    {"wa_id": "37060000001", "profile_name": "Ops"},
                    {"wa_id": "37070000002", "profile_name": "New Person"},
                ]
            },
        }
    }
    contacts, _ = main_queries._handle_query(
        {"query_id": "list_contacts", "context": {"project_id": "7"}, "runtime_context": context}
    )
    assert contacts["total"] == 1
    assert contacts["items"][0]["_meta"]["immutable_fields"] == ["channel_id"]
    senders, _ = main_queries._handle_query(
        {
            "query_id": "list_discovered_senders",
            "context": {"project_id": "7"},
            "runtime_context": context,
        }
    )
    by_id = {row["wa_id"]: row for row in senders["items"]}
    assert by_id["37060000001"]["already_saved"] is True
    assert by_id["37070000002"]["already_saved"] is False
    assert by_id["37070000002"]["suggested_channel_id"] == "new_person"


def test_save_discovered_sender_adds_a_closed_contact():
    result, code = main_actions._handle_action(
        {
            "action_id": "save_discovered_sender",
            "context": {"project_id": "7"},
            "input": {"wa_id": "37070000002", "display_name": "New Person"},
            "runtime_context": {
                "project_connection": {"connection_id": "c", "connection_config": CONFIG}
            },
        }
    )
    assert code == 0, result
    new = [c for c in result["values"]["contacts"] if c["wa_id"] == "37070000002"][0]
    assert new["capabilities"] == {"notify": True, "workflow_trigger": False}


def test_save_discovered_sender_refuses_a_duplicate():
    result, _ = main_actions._handle_action(
        {
            "action_id": "save_discovered_sender",
            "context": {"project_id": "7"},
            "input": {"wa_id": "37060000001"},
            "runtime_context": {
                "project_connection": {"connection_id": "c", "connection_config": CONFIG}
            },
        }
    )
    assert result["error"] == "contact_already_exists"


# --- readiness ------------------------------------------------------------------------------------


def readiness(**overrides):
    base = dict(
        connection={"connection_id": "c"},
        token_present=True,
        app_secret_present=True,
        phone_number_id="1",
        identity_response={
            "ok": True,
            "data": {"display_phone_number": "+1 555", "verified_name": "Shop"},
        },
        contacts=main_actions._contact_items_from_config(CONFIG),
        registration={"id": "r"},
        inbound_state={},
        deliveries=[],
    )
    base.update(overrides)
    return main_readiness.derive_readiness(**base)


def test_readiness_states():
    assert (
        readiness()["summary"]["readiness"] == "needs_attention"
    )  # registered, nothing received yet
    assert readiness(inbound_state={"last_inbound_at": "now"})["summary"]["readiness"] == "ready"
    assert readiness(registration={}, app_secret_present=False)["summary"]["readiness"] == "ready"
    assert readiness(token_present=False)["summary"]["readiness"] == "setup_incomplete"
    assert readiness(phone_number_id="")["summary"]["readiness"] == "setup_incomplete"
    assert readiness(contacts=[])["summary"]["readiness"] == "setup_incomplete"
    rejected = readiness(identity_response={"ok": False, "error_code": "whatsapp_token_invalid"})
    assert rejected["summary"]["readiness"] == "failed"
    unreachable = readiness(identity_response={"ok": False, "error_code": "whatsapp_unreachable"})
    assert unreachable["summary"]["readiness"] == "needs_attention"


def test_readiness_never_contains_a_secret():
    assert TOKEN not in json.dumps(readiness())
