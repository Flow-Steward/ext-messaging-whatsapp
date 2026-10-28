"""Inbound webhooks: parsing every message type, statuses, dedupe and closed-by-default gating."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from conftest import PHONE_NUMBER_ID

import main_ingress
import main_state

ALLOWED = "37060000001"
STRANGER = "37099999999"


def contact(wa_id: str = ALLOWED, *, trigger: bool = True, enabled: bool = True) -> dict[str, Any]:
    return {
        "channel_id": "ops",
        "display_name": "Ops",
        "wa_id": wa_id,
        "enabled": enabled,
        "capabilities": {"notify": True, "workflow_trigger": trigger},
    }


def envelope(
    *,
    messages: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    phone_number_id: str = PHONE_NUMBER_ID,
    profiles: dict[str, str] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550783881", "phone_number_id": phone_number_id},
    }
    if messages is not None:
        value["contacts"] = [
            {"profile": {"name": name}, "wa_id": wa}
            for wa, name in (profiles or {ALLOWED: "Ann"}).items()
        ]
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def message(
    kind: str,
    body: dict[str, Any] | None,
    *,
    wamid: str = "wamid.1",
    sender: str = ALLOWED,
    **extra,
):
    base: dict[str, Any] = {"from": sender, "id": wamid, "timestamp": "1700000000", "type": kind}
    if body is not None:
        base[kind] = body
    base.update(extra)
    return base


def request(
    payload: dict[str, Any],
    *,
    contacts: list[dict[str, Any]] | None = None,
    ingress_config: dict[str, Any] | None = None,
    authenticated: bool = True,
) -> dict[str, Any]:
    return {
        "method": "POST",
        "json": payload,
        "callback": {"authenticated": authenticated, "project_id": "7", "connection_id": "conn-1"},
        "provider": {
            "provider_config": {
                "phone_number_id": PHONE_NUMBER_ID,
                "contacts": contacts if contacts is not None else [contact()],
            },
            "ingress_config": ingress_config or {},
            "project_id": "7",
            "connection_id": "conn-1",
        },
    }


def events_of(req: dict[str, Any]) -> list[dict[str, Any]]:
    result = main_ingress._ingress_event_response(req)
    assert result["ok"] is True, result
    return result["events"]


def one_payload(kind: str, body: dict[str, Any] | None, **extra: Any) -> dict[str, Any]:
    events = events_of(request(envelope(messages=[message(kind, body, **extra)])))
    assert len(events) == 1
    assert events[0]["event_type"] == "whatsapp.message.received"
    return events[0]["payload"]


def test_text_message_event_has_the_documented_fields():
    events = events_of(request(envelope(messages=[message("text", {"body": "Hi there"})])))
    event = events[0]
    assert event["project_id"] == "7"
    assert event["correlation_id"] == "wamid.1"
    payload = event["payload"]
    assert payload["text"] == "Hi there"
    assert payload["message_type"] == "text"
    assert payload["wa_id"] == ALLOWED
    assert payload["profile_name"] == "Ann"
    assert payload["message_id"] == "wamid.1"
    assert payload["reply_to_message_id"] == ""
    assert payload["has_file"] is False
    assert payload["file_id"] == ""
    assert payload["timestamp"] == 1700000000
    assert payload["phone_number_id"] == PHONE_NUMBER_ID
    assert payload["dedupe_key"] == "wamid.1"
    assert payload["project_id"] == "7"
    assert payload["connection_id"] == "conn-1"


def test_reply_context_becomes_reply_to_message_id():
    payload = one_payload("text", {"body": "x"}, context={"from": "15550783881", "id": "wamid.OUT"})
    assert payload["reply_to_message_id"] == "wamid.OUT"


@pytest.mark.parametrize("kind", ["image", "video"])
def test_image_and_video(kind):
    payload = one_payload(
        kind, {"caption": "cap", "mime_type": f"{kind}/x", "sha256": "h", "id": "MEDIA1"}
    )
    assert payload["has_file"] is True
    assert payload["file_id"] == "MEDIA1"
    assert payload["file_kind"] == kind
    assert payload["mime_type"] == f"{kind}/x"
    assert payload["caption"] == "cap"
    assert payload["text"] == "cap"


def test_document_carries_the_file_name():
    payload = one_payload(
        "document",
        {
            "filename": "receipt.pdf",
            "caption": "my receipt",
            "mime_type": "application/pdf",
            "sha256": "h",
            "id": "D1",
        },
    )
    assert payload["file_kind"] == "document"
    assert payload["file_name"] == "receipt.pdf"
    assert payload["file_id"] == "D1"


def test_audio_and_voice_flag():
    payload = one_payload(
        "audio", {"mime_type": "audio/ogg; codecs=opus", "sha256": "h", "id": "A1", "voice": True}
    )
    assert payload["file_kind"] == "audio"
    assert payload["is_voice"] is True
    assert payload["mime_type"] == "audio/ogg; codecs=opus"


def test_button_reply():
    payload = one_payload(
        "interactive",
        {"type": "button_reply", "button_reply": {"id": "cancel-button", "title": "Cancel"}},
        context={"from": "15550783881", "id": "wamid.OUT"},
    )
    assert payload["message_type"] == "interactive"
    assert payload["button_id"] == "cancel-button"
    assert payload["button_title"] == "Cancel"
    assert payload["text"] == "Cancel"
    assert payload["reply_to_message_id"] == "wamid.OUT"
    assert payload["has_file"] is False


def test_list_reply():
    payload = one_payload(
        "interactive",
        {
            "type": "list_reply",
            "list_reply": {"id": "priority_express", "title": "Express", "description": "Next day"},
        },
    )
    assert payload["button_id"] == "priority_express"
    assert payload["list_description"] == "Next day"


def test_location():
    payload = one_payload(
        "location",
        {
            "address": "1 Main St",
            "latitude": 54.68,
            "longitude": 25.27,
            "name": "Office",
            "url": "u",
        },
    )
    assert payload["message_type"] == "location"
    assert payload["location_latitude"] == 54.68
    assert payload["location_longitude"] == 25.27
    assert payload["location_name"] == "Office"
    assert payload["location_address"] == "1 Main St"


def test_reaction_and_its_removal():
    payload = one_payload("reaction", {"message_id": "wamid.OUT", "emoji": "\U0001f44d"})
    assert payload["message_type"] == "reaction"
    assert payload["reaction_message_id"] == "wamid.OUT"
    assert payload["reaction_emoji"] == "\U0001f44d"
    removed = one_payload("reaction", {"message_id": "wamid.OUT"}, wamid="wamid.2")
    assert removed["reaction_emoji"] == ""


def test_an_unsupported_type_is_reported_without_a_file():
    payload = one_payload("unsupported", None, errors=[{"code": 131051, "title": "x"}])
    assert payload["message_type"] == "unsupported"
    assert payload["has_file"] is False


# --- gating ---------------------------------------------------------------------------


def test_closed_by_default_unknown_sender_starts_nothing_but_is_discovered(monkeypatch):
    saved = _capture_saves(monkeypatch)
    req = request(
        envelope(
            messages=[message("text", {"body": "x"}, sender=STRANGER)],
        ),
        contacts=[contact()],
    )
    result = main_ingress._ingress_event_response(req)
    assert result["events"] == []
    assert result["result"]["not_allowed"] == 1
    state = saved[-1]["ingress_config"]
    assert [row["wa_id"] for row in state["discovered_senders"]] == [STRANGER]


def test_a_saved_contact_without_workflow_trigger_starts_nothing():
    req = request(
        envelope(messages=[message("text", {"body": "x"})]), contacts=[contact(trigger=False)]
    )
    assert events_of(req) == []


def test_a_disabled_contact_starts_nothing():
    req = request(
        envelope(messages=[message("text", {"body": "x"})]), contacts=[contact(enabled=False)]
    )
    assert events_of(req) == []


def test_a_contact_without_the_capability_key_is_closed():
    raw = contact()
    del raw["capabilities"]["workflow_trigger"]
    req = request(envelope(messages=[message("text", {"body": "x"})]), contacts=[raw])
    assert events_of(req) == []


def test_sender_matching_ignores_phone_formatting():
    req = request(
        envelope(messages=[message("text", {"body": "x"})]), contacts=[contact("+370 600-00001")]
    )
    assert len(events_of(req)) == 1


def test_a_message_to_another_phone_number_is_ignored():
    payload = envelope(messages=[message("text", {"body": "x"})], phone_number_id="999")
    result = main_ingress._ingress_event_response(request(payload))
    assert result["events"] == []
    assert result["result"]["other_number"] == 1


def test_unauthenticated_requests_are_refused():
    result = main_ingress._ingress_event_response(
        request(envelope(messages=[message("text", {"body": "x"})]), authenticated=False)
    )
    assert result["ok"] is False
    assert result["error"] == "unauthorized_whatsapp_ingress"
    assert result["events"] == []


def test_a_foreign_object_and_a_get_are_ignored():
    req = request({"object": "page", "entry": []})
    assert main_ingress._ingress_event_response(req)["events"] == []
    req = request(envelope(messages=[message("text", {"body": "x"})]))
    req["method"] = "GET"
    assert main_ingress._ingress_event_response(req)["events"] == []


def test_a_field_other_than_messages_is_ignored():
    payload = envelope(messages=[message("text", {"body": "x"})])
    payload["entry"][0]["changes"][0]["field"] = "account_update"
    assert events_of(request(payload)) == []


# --- statuses ------------------------------------------------------------------------------


def status(state: str, wamid: str = "wamid.OUT", **extra: Any) -> dict[str, Any]:
    return {
        "id": wamid,
        "status": state,
        "timestamp": "1700000100",
        "recipient_id": ALLOWED,
        **extra,
    }


@pytest.mark.parametrize("state", ["sent", "delivered", "read", "failed"])
def test_status_events_are_emitted_for_every_status(state):
    events = events_of(request(envelope(statuses=[status(state)]), contacts=[]))
    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "whatsapp.message.status"
    payload = event["payload"]
    assert payload["status"] == state
    assert payload["message_id"] == "wamid.OUT"
    assert payload["wa_id"] == ALLOWED
    assert payload["dedupe_key"] == f"wamid.OUT:{state}"
    assert payload["timestamp"] == 1700000100


def test_failed_status_carries_the_error():
    failed = status(
        "failed",
        errors=[
            {
                "code": 131047,
                "title": "Re-engagement",
                "message": "m",
                "error_data": {"details": "d"},
            }
        ],
    )
    payload = events_of(request(envelope(statuses=[failed])))[0]["payload"]
    assert payload["error_code"] == 131047
    assert payload["error_title"] == "Re-engagement"
    assert payload["error_details"] == "d"
    assert events_of(request(envelope(statuses=[failed])))[0]["severity"] == "warning"


# --- dedupe --------------------------------------------------------------------------------------


def _capture_saves(monkeypatch):
    saved: list[dict[str, Any]] = []

    class Store:
        def get_project_connection(self, **_kw):
            return {"connection_id": "conn-1"}

        def upsert_project_connection(self, **kw):
            saved.append(kw)

    monkeypatch.setattr(main_ingress, "_runtime_logger", lambda _ctx: Store())
    monkeypatch.setattr(
        main_state, "_connection_row_for_project", lambda _bl, _p: {"connection_id": "conn-1"}
    )
    return saved


def test_a_meta_retry_does_not_emit_twice(monkeypatch):
    saved = _capture_saves(monkeypatch)
    payload = envelope(messages=[message("text", {"body": "x"})], statuses=[status("sent")])
    first = main_ingress._ingress_event_response(request(payload))
    assert len(first["events"]) == 2
    state = saved[-1]["ingress_config"]
    assert set(state["seen_message_keys"]) == {"wamid.1", "wamid.OUT:sent"}

    retry = main_ingress._ingress_event_response(
        request(copy.deepcopy(payload), ingress_config=state)
    )
    assert retry["events"] == []
    assert retry["result"]["duplicates"] == 2


def test_the_same_message_twice_in_one_delivery_emits_once():
    payload = envelope(messages=[message("text", {"body": "x"}), message("text", {"body": "x"})])
    assert len(events_of(request(payload))) == 1


def test_a_later_status_of_the_same_message_is_not_a_duplicate():
    state = {"seen_message_keys": ["wamid.OUT:sent"]}
    events = events_of(
        request(envelope(statuses=[status("sent"), status("delivered")]), ingress_config=state)
    )
    assert [e["payload"]["status"] for e in events] == ["delivered"]


def test_seen_keys_are_bounded(monkeypatch):
    saved = _capture_saves(monkeypatch)
    state = {"seen_message_keys": [f"old.{i}" for i in range(600)]}
    main_ingress._ingress_event_response(
        request(envelope(messages=[message("text", {"body": "x"})]), ingress_config=state)
    )
    keys = saved[-1]["ingress_config"]["seen_message_keys"]
    assert len(keys) == 500
    assert keys[-1] == "wamid.1"


def test_discovered_senders_keep_the_profile_name(monkeypatch):
    saved = _capture_saves(monkeypatch)
    main_ingress._ingress_event_response(
        request(envelope(messages=[message("text", {"body": "x"})], profiles={ALLOWED: "Ann"}))
    )
    row = saved[-1]["ingress_config"]["discovered_senders"][0]
    assert (row["wa_id"], row["profile_name"], row["last_message_type"]) == (ALLOWED, "Ann", "text")


def test_the_handler_reads_the_provider_from_the_runtime_resources():
    action_payload = {
        "request": {
            "json": envelope(messages=[message("text", {"body": "x"})]),
            "callback": {"authenticated": True},
        },
        "runtime_context": {
            "resources": {
                "project_connection": {
                    "connection_id": "c9",
                    "project_id": "7",
                    "connection_config": {
                        "phone_number_id": PHONE_NUMBER_ID,
                        "contacts": [contact()],
                    },
                }
            }
        },
    }
    result, code = main_ingress._handle_ingress_event_payload(action_payload)
    assert code == 0
    assert len(result["events"]) == 1
    assert result["events"][0]["payload"]["connection_id"] == "c9"
