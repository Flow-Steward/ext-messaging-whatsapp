from __future__ import annotations

from pathlib import Path

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_STEPS = [
    "send_message",
    "send_template",
    "send_reaction",
    "mark_as_read",
    "send_file",
    "get_file",
]
REMOVED_TELEGRAM_STEPS = {"edit_message", "delete_message", "pin_message", "unpin_message"}


def _load(relative_path: str):
    return yaml.safe_load((BUNDLE_ROOT / relative_path).read_text(encoding="utf-8"))


def test_declares_one_whatsapp_cloud_connection_type() -> None:
    connection_types = _load("contracts/connection_types.yaml")["connection_types"]
    assert [row["connection_type_id"] for row in connection_types] == ["whatsapp_cloud"]
    row = connection_types[0]
    properties = row["config_schema"]["properties"]
    assert {"phone_number_id", "waba_id", "graph_api_version", "contacts"} <= set(properties)
    assert properties["graph_api_version"]["default"] == "v26.0"
    assert row["secret_schema"]["required"] == ["access_token"]
    assert set(row["secret_schema"]["properties"]) == {"access_token", "app_secret"}
    capabilities = row["capabilities"]
    # Approvals through buttons are out of scope for 0.1.0; there is no polling.
    assert capabilities["human_decisions"] is False
    assert capabilities["polling_ingress"] is False
    assert capabilities["webhook_ingress"] is True
    contact = properties["contacts"]["items"]
    assert set(contact["properties"]["capabilities"]["properties"]) == {
        "notify",
        "workflow_trigger",
    }
    assert (
        contact["properties"]["capabilities"]["properties"]["workflow_trigger"]["default"] is False
    )
    assert contact["properties"]["wa_id"]["pattern"] == "^[0-9]{7,15}$"


def test_only_the_message_steps_are_workflow_steps_and_none_edits_or_deletes() -> None:
    operations = _load("contracts/operation_manifest.yaml")["operations"]
    actions = _load("ui/actions/actions.yaml")["actions"]
    forms = _load("contracts/step_ui_manifest.yaml")["forms"]

    assert [r["operation_id"] for r in operations if r.get("workflow_visible")] == WORKFLOW_STEPS
    assert [r["action_id"] for r in actions if r.get("workflow_visible")] == WORKFLOW_STEPS
    assert [f["operation_id"] for f in forms] == WORKFLOW_STEPS
    every_id = {r["operation_id"] for r in operations} | {r["action_id"] for r in actions}
    assert not (every_id & REMOVED_TELEGRAM_STEPS)


def test_delivery_operation_is_internal() -> None:
    operations = _load("contracts/operation_manifest.yaml")["operations"]
    delivery = [r for r in operations if r["operation_id"] == "deliver_notification"]
    assert len(delivery) == 1
    assert delivery[0]["workflow_visible"] is False


def test_external_effects_are_idempotent_and_suppressed_in_test_runs() -> None:
    manifest = _load("extension.yaml")
    effects = {row["operation_id"]: row for row in manifest["external_effects"]}
    # get_file only reads from WhatsApp, so it is no external effect.
    assert set(effects) == {"deliver_notification", *WORKFLOW_STEPS} - {"get_file"}
    for effect in effects.values():
        assert effect["channel"] == "whatsapp"
        assert effect["test_mode_behavior"] == "suppress"
        assert effect["idempotency"] == "required"
    actions = {r["action_id"]: r for r in _load("ui/actions/actions.yaml")["actions"]}
    delivery = actions["deliver_notification"]
    assert delivery["handler"]["config"]["host_mutations"] == ["project_connection_upsert"]
    assert delivery["mutates_platform"] is True


def test_callback_declares_the_meta_hub_signature_and_both_methods() -> None:
    manifest = _load("extension.yaml")
    callbacks = manifest["callbacks"]
    assert len(callbacks) == 1
    callback = callbacks[0]
    assert callback["callback_id"] == "updates"
    assert callback["methods"] == ["GET", "POST"]
    assert callback["action_id"] == "ingress_event"
    assert callback["connection_scoped"] is True
    assert callback["auth"] == {
        "type": "hub_signature",
        "signature_header": "X-Hub-Signature-256",
        "secret_name": "app_secret",
    }


def test_triggers_and_event_subscriptions() -> None:
    manifest = _load("extension.yaml")
    triggers = {t["trigger_type_id"]: t for t in manifest["workflow_triggers"]}
    assert set(triggers) == {"message_received", "message_status"}
    assert triggers["message_received"]["event_name"] == "whatsapp.message.received"
    assert triggers["message_status"]["event_name"] == "whatsapp.message.status"
    status = triggers["message_status"]["config_schema"]["properties"]["status"]
    assert status["enum"] == ["sent", "delivered", "read", "failed"]
    received = set(triggers["message_received"]["config_schema"]["properties"])
    assert {"wa_id", "message_type", "has_file"} <= received
    subscribed = {row["event_type"] for row in manifest["event_subscriptions"]}
    assert {"whatsapp.message.received", "whatsapp.message.status"} <= subscribed
    assert not {e for e in subscribed if e.startswith("approval")}


def test_identity_and_egress() -> None:
    manifest = _load("extension.yaml")
    assert manifest["extension_id"] == "flowsteward.whatsapp"
    assert manifest["display_name"] == "WhatsApp"
    assert manifest["version"] == "0.1.0"
    assert manifest["kind"] == "notification_provider"
    assert "graph.facebook.com" in manifest["egress"]["hosts"]
    grants = manifest["runtime_grants"]["mutations"]["project_connection_secrets_save"]
    assert grants["secret_keys"] == ["access_token", "app_secret"]
    assert grants["connection_type_ids"] == ["whatsapp_cloud"]


def test_no_telegram_left_in_the_bundle() -> None:
    skipped = {"docs", ".git", "dev-wheels", "tests"}
    for path in BUNDLE_ROOT.rglob("*"):
        if not path.is_file() or any(
            part in skipped for part in path.relative_to(BUNDLE_ROOT).parts
        ):
            continue
        if path.suffix in {".png", ".whl", ".pyc"}:
            continue
        assert "telegram" not in path.read_text(encoding="utf-8", errors="ignore").lower(), path


def test_step_error_codes_cover_the_mapped_graph_errors() -> None:
    operations = {
        r["operation_id"]: r for r in _load("contracts/operation_manifest.yaml")["operations"]
    }
    codes = set(operations["send_message"]["error_codes"])
    assert {"whatsapp_window_closed", "whatsapp_rate_limited", "whatsapp_token_invalid"} <= codes
    assert {"message_too_long", "message_too_long_for_buttons", "buttons_invalid"} <= codes
