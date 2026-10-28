"""UI pages, components, actions and locales stay consistent with the contracts."""

from __future__ import annotations

from pathlib import Path

import yaml

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
UI = BUNDLE_ROOT / "ui"


def load(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def page_components(page_id: str) -> list[dict]:
    page = load(UI / "pages" / f"{page_id}.yaml")
    return [load((UI / "pages" / item["ref"]).resolve()) for item in page["components"]]


def test_ui_is_four_pages_in_setup_order_without_inbound_mode_or_polling():
    manifest = load(UI / "ui_manifest.yaml")
    ids = [load(UI / item["ref"])["page_id"] for item in manifest["pages"]]
    assert ids == ["overview", "setup", "contacts", "deliveries"]
    for stale in (
        "inbound_mode_form",
        "inbound_status_grid",
        "chat_discovery_form",
        "channels_form",
    ):
        assert not (UI / "components" / f"{stale}.yaml").exists()


def test_overview_is_read_only():
    components = page_components("overview")
    assert all(c["type"] != "form" for c in components)
    assert any(
        c["type"] == "health_panel" and c["query_id"] == "provider_health" for c in components
    )


def test_setup_has_credentials_callback_and_checklist_in_order():
    components = page_components("setup")
    ids = [c["component_id"] for c in components]
    assert ids[0] == "secrets_form"
    assert (
        ids.index("secrets_form") < ids.index("config_form") < ids.index("whatsapp_webhook_setup")
    )
    secrets = components[0]
    assert [f["field_id"] for f in secrets["ui_schema"]["fields"]] == ["access_token", "app_secret"]
    assert all(f["widget"] == "secret" for f in secrets["ui_schema"]["fields"])
    assert secrets["data"]["save_action"] == "save_secrets"
    webhook = components[ids.index("whatsapp_webhook_setup")]
    assert webhook["type"] == "callback_setup"
    assert webhook["callback_id"] == "updates"
    checklist = components[ids.index("quick_start")]["content"].lower()
    for topic in ("system user", "app secret", "phone number id", "verify token", "messages"):
        assert topic in checklist, topic
    guide = components[ids.index("setup_guide")]
    assert guide.get("collapsible") is True
    assert "24-hour" in guide["content"]


def test_contacts_page_explains_then_asks():
    components = page_components("contacts")
    ids = [c["component_id"] for c in components]
    assert ids == [
        "contacts_intro",
        "contacts_form",
        "discovered_senders_grid",
        "contact_test_grid",
    ]
    form = components[1]
    assert form["data"] == {
        "load_query": "list_contacts",
        "save_action": "upsert_contact",
        "save_mode": "replace",
    }
    repeater = form["ui_schema"]["fields"][0]
    paths = [f["path"] for f in repeater["props"]["fields"]]
    assert paths == [
        "channel_id",
        "display_name",
        "wa_id",
        "enabled",
        "capabilities.notify",
        "capabilities.workflow_trigger",
    ]
    trigger = repeater["props"]["fields"][-1]
    assert trigger["default"] is False
    grid = components[2]
    assert grid["row_actions"][0]["action_id"] == "save_discovered_sender"


def test_every_component_binding_exists():
    actions = {a["action_id"] for a in load(UI / "actions" / "actions.yaml")["actions"]}
    queries = {q["query_id"] for q in load(UI / "queries" / "queries.yaml")["queries"]}
    for path in sorted((UI / "components").glob("*.yaml")):
        component = load(path)
        if component.get("query_id"):
            assert component["query_id"] in queries, path.name
        data = component.get("data") or {}
        for key in ("load_query",):
            if data.get(key):
                assert data[key] in queries, path.name
        if data.get("save_action"):
            assert data["save_action"] in actions, path.name
        for row_action in component.get("row_actions") or []:
            assert row_action["action_id"] in actions, path.name
        for key in ("configure_action_id", "status_action_id"):
            if component.get(key):
                assert component[key] in actions, path.name
        for entry in ((component.get("ui_schema") or {}).get("actions") or {}).values():
            if isinstance(entry, dict):
                assert entry["action_id"] in actions, path.name


def test_actions_declare_the_inputs_their_handlers_consume():
    actions = {a["action_id"]: a for a in load(UI / "actions" / "actions.yaml")["actions"]}
    expected = {
        "test_delivery": {"channel_id"},
        "save_config": {"phone_number_id", "waba_id", "graph_api_version"},
        "save_secrets": {"access_token", "app_secret"},
        "save_discovered_sender": {"wa_id", "channel_id", "display_name", "profile_name"},
        "upsert_contact": {"contacts"},
        "toggle_contact": {"channel_id"},
        "delete_contact": {"channel_id"},
    }
    for action_id, names in expected.items():
        assert {p["name"] for p in actions[action_id]["parameters"]} == names, action_id
    secrets = actions["save_secrets"]
    # the token-saving path is the platform operation Core stores in provider_secrets
    assert secrets["handler"]["operation"] == "project_connection_secrets_save"
    assert secrets["handler"]["config"]["connection_type_id"] == "whatsapp_cloud"
    assert [f["secret_key"] for f in secrets["handler"]["config"]["secret_fields"]] == [
        "access_token",
        "app_secret",
    ]


def test_locales_en_and_lt_exist_and_lt_has_no_dead_keys():
    en = load(BUNDLE_ROOT / "locales" / "en.yaml")
    lt = load(BUNDLE_ROOT / "locales" / "lt.yaml")
    assert en["ext.display_name"] == "WhatsApp"
    assert set(lt) <= set(en)
    assert all(isinstance(v, str) for v in {**en, **lt}.values())
    assert lt["page.contacts.title"] == "Kontaktai"
    for step in (
        "send_message",
        "send_template",
        "send_reaction",
        "mark_as_read",
        "send_file",
        "get_file",
    ):
        assert f"step.{step}.section" in en
        assert f"op.{step}.display_name" in en
