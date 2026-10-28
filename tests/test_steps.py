"""Every workflow step, with the one transport function mocked (never the network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import MEDIA_URL, PHONE_NUMBER_ID, TOKEN, graph_error, ok_message

import main_send
import main_steps


def contact(
    channel_id: str = "alerts",
    wa_id: str = "37060000001",
    *,
    notify: bool = True,
    enabled: bool = True,
    trigger: bool = False,
) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "display_name": channel_id.title(),
        "wa_id": wa_id,
        "enabled": enabled,
        "capabilities": {"notify": notify, "workflow_trigger": trigger},
    }


def action(
    action_id: str,
    payload: dict[str, Any],
    *,
    contacts: list[dict[str, Any]] | None = None,
    token: str = TOKEN,
    test_mode: bool = False,
    artifacts: dict[str, Any] | None = None,
    phone_number_id: str = PHONE_NUMBER_ID,
) -> dict[str, Any]:
    built: dict[str, Any] = {
        "action_id": action_id,
        "context": {"project_id": "7", "test_mode": test_mode},
        "target": {
            "connection": {
                "connection_id": "conn-wa",
                "config": {
                    "phone_number_id": phone_number_id,
                    "contacts": contacts if contacts is not None else [contact()],
                },
                "secrets": {"access_token": token},
            }
        },
        "input": payload,
    }
    if artifacts is not None:
        built["artifacts"] = artifacts
    return built


def send_text(payload: dict[str, Any], **kwargs: Any):
    return main_send.handle_send_message(action("send_message", payload, **kwargs), {})


# --- send_message -------------------------------------------------------------


def test_send_message_posts_a_text_and_returns_the_wamid(graph):
    graph.reply(200, ok_message("wamid.A"))
    result, code = send_text({"text": "Hello"})
    assert code == 0
    assert result["delivered"] is True
    assert result["provider_message_id"] == "wamid.A"
    assert result["channel_id"] == "alerts"
    assert result["external_effect_status"] == "succeeded"
    assert graph.json_body() == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "37060000001",
        "type": "text",
        "text": {"body": "Hello"},
    }
    assert (
        graph.requests[0]["url"] == f"https://graph.facebook.com/v26.0/{PHONE_NUMBER_ID}/messages"
    )


def test_send_message_uses_the_configured_graph_version(graph):
    graph.reply(200, ok_message())
    built = action("send_message", {"text": "x"})
    built["target"]["connection"]["config"]["graph_api_version"] = "v30.1"
    main_send.handle_send_message(built, {})
    assert "/v30.1/" in graph.requests[0]["url"]


def test_send_message_preview_url_and_reply_context(graph):
    graph.reply(200, ok_message())
    result, code = send_text(
        {"text": "see https://example.com", "preview_url": True, "reply_to_message_id": "wamid.IN"}
    )
    assert code == 0, result
    body = graph.json_body()
    assert body["text"] == {"body": "see https://example.com", "preview_url": True}
    assert body["context"] == {"message_id": "wamid.IN"}


def test_send_message_boundaries_4096_ok_4097_refused_never_cut(graph):
    graph.reply(200, ok_message())
    result, code = send_text({"text": "a" * 4096})
    assert code == 0, result
    assert len(graph.json_body()["text"]["body"]) == 4096
    result, code = send_text({"text": "a" * 4097})
    assert (code, result["error_code"]) == (2, "message_too_long")
    assert result["definitely_no_external_effect"] is True
    assert len(graph.requests) == 1


@pytest.mark.parametrize("text", [None, "", "   ", 5])
def test_send_message_needs_text(text):
    result, code = send_text({"text": text})
    assert (code, result["error_code"]) == (2, "text_required")


def test_send_message_with_buttons_becomes_interactive(graph):
    graph.reply(200, ok_message())
    result, code = send_text(
        {
            "text": "Approve?",
            "buttons": [{"id": "yes", "title": "Yes"}, {"id": "no", "title": "No"}],
        }
    )
    assert code == 0, result
    body = graph.json_body()
    assert body["type"] == "interactive"
    assert "text" not in body
    assert body["interactive"] == {
        "type": "button",
        "body": {"text": "Approve?"},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": "yes", "title": "Yes"}},
                {"type": "reply", "reply": {"id": "no", "title": "No"}},
            ]
        },
    }


def test_buttons_may_arrive_as_a_json_string(graph):
    graph.reply(200, ok_message())
    result, code = send_text({"text": "x", "buttons": json.dumps([{"id": "a", "title": "A"}])})
    assert code == 0, result
    assert graph.json_body()["type"] == "interactive"


def test_button_body_limit_is_1024_and_never_cut(graph):
    graph.reply(200, ok_message())
    buttons = [{"id": "a", "title": "A"}]
    result, code = send_text({"text": "b" * 1024, "buttons": buttons})
    assert code == 0, result
    result, code = send_text({"text": "b" * 1025, "buttons": buttons})
    assert (code, result["error_code"]) == (2, "message_too_long_for_buttons")
    assert len(graph.requests) == 1


@pytest.mark.parametrize(
    "buttons",
    [
        [{"id": str(i), "title": f"T{i}"} for i in range(4)],  # four buttons
        [{"id": "a", "title": "x" * 21}],  # title too long
        [{"id": "a", "title": "same"}, {"id": "b", "title": "same"}],  # duplicate title
        [{"id": "a", "title": "A"}, {"id": "a", "title": "B"}],  # duplicate id
        [{"id": "x" * 257, "title": "A"}],  # id too long
        [{"id": "", "title": "A"}],
        [{"id": "a", "title": ""}],
        "not json",
        {"id": "a"},
    ],
)
def test_invalid_buttons_are_refused_early(buttons):
    result, code = send_text({"text": "x", "buttons": buttons})
    assert (code, result["error_code"]) == (2, "buttons_invalid")


def test_three_buttons_and_a_20_character_title_are_fine(graph):
    graph.reply(200, ok_message())
    buttons = [{"id": "1", "title": "t" * 20}, {"id": "2", "title": "B"}, {"id": "3", "title": "C"}]
    result, code = send_text({"text": "x", "buttons": buttons})
    assert code == 0, result


def test_contact_selection_rules(graph):
    contacts = [contact("a", "37060000001"), contact("b", "37060000002")]
    result, _ = send_text({"text": "x"}, contacts=contacts)
    assert result["error_code"] == "channel_id_required"
    result, _ = send_text({"text": "x", "channel_id": "zzz"}, contacts=contacts)
    assert result["error_code"] == "unknown_channel_id"
    result, _ = send_text({"text": "x", "wa_id": "37099999999"}, contacts=contacts)
    assert result["error_code"] == "unknown_wa_id"
    result, _ = send_text({"text": "x", "wa_id": "12"}, contacts=contacts)
    assert result["error_code"] == "wa_id_invalid"
    result, _ = send_text({"text": "x"}, contacts=[])
    assert result["error_code"] == "no_notify_contact"
    result, _ = send_text({"text": "x", "channel_id": "a"}, contacts=[contact("a", enabled=False)])
    assert result["error_code"] == "channel_disabled"
    result, _ = send_text({"text": "x", "channel_id": "a"}, contacts=[contact("a", notify=False)])
    assert result["error_code"] == "channel_missing_notify"
    assert graph.requests == []


def test_contact_can_be_chosen_by_phone_number_with_formatting(graph):
    graph.reply(200, ok_message())
    contacts = [contact("a", "37060000001"), contact("b", "37060000002")]
    result, code = send_text({"text": "x", "wa_id": "+370 600-00002"}, contacts=contacts)
    assert code == 0, result
    assert graph.json_body()["to"] == "37060000002"
    assert result["channel_id"] == "b"


def test_a_single_notify_contact_is_the_default(graph):
    graph.reply(200, ok_message())
    contacts = [contact("a", notify=False), contact("b", "37060000002")]
    result, code = send_text({"text": "x"}, contacts=contacts)
    assert code == 0, result
    assert result["channel_id"] == "b"


def test_a_missing_connection_is_refused():
    built = action("send_message", {"text": "x"})
    built["target"] = {}
    result, code = main_send.handle_send_message(built, {})
    assert (code, result["error_code"]) == (2, "project_connection_required")


def test_missing_phone_number_id_is_refused_before_a_request(graph):
    result, code = send_text({"text": "x"}, phone_number_id="")
    assert (code, result["error_code"]) == (2, "phone_number_id_required")
    assert graph.requests == []


def test_test_run_suppresses_every_send_step(graph):
    for step_id, handler, payload in (
        ("send_message", main_send.handle_send_message, {"text": "x"}),
        ("send_template", main_send.handle_send_template, {"template_name": "t", "language": "en"}),
        ("send_reaction", main_send.handle_send_reaction, {"message_id": "w", "emoji": "x"}),
        ("mark_as_read", main_send.handle_mark_as_read, {"message_id": "w"}),
        ("send_file", main_steps.handle_send_file, {"kind": "image", "artifact_handle": "a"}),
    ):
        result, code = handler(action(step_id, payload, test_mode=True), {})
        assert code == 0, (step_id, result)
        assert result["external_effect_status"] == "suppressed"
    assert graph.requests == []


def test_runtime_context_test_mode_also_suppresses(graph):
    result, code = main_send.handle_send_message(
        action("send_message", {"text": "x"}), {"test_mode": True}
    )
    assert code == 0
    assert result["external_effect_status"] == "suppressed"
    assert graph.requests == []


def test_window_closed_is_surfaced_with_advice(graph):
    graph.reply(400, graph_error(131047))
    result, code = send_text({"text": "x"})
    assert (code, result["error_code"]) == (2, "whatsapp_window_closed")
    assert "template" in result["error"]
    assert result["delivered"] is False
    assert result["definitely_no_external_effect"] is True


def test_rate_limit_is_flagged_retryable(graph):
    graph.reply(429, graph_error(130429))
    result, code = send_text({"text": "x"})
    assert (code, result["error_code"], result["retryable"]) == (2, "whatsapp_rate_limited", True)


def test_ambiguous_failure_passes_through(graph):
    graph.fail(TimeoutError("t"))
    result, code = send_text({"text": "x"})
    assert (code, result["error_code"]) == (2, "whatsapp_send_ambiguous")
    assert result["external_effect_status"] == "timeout_unknown"
    assert result["definitely_no_external_effect"] is False


def test_the_token_never_reaches_a_result(graph):
    graph.reply(401, graph_error(190, f"token {TOKEN} is bad"))
    result, _ = send_text({"text": "x"})
    assert TOKEN not in json.dumps(result)


# --- send_template ---------------------------------------------------------------


def test_send_template_with_positional_body_parameters(graph):
    graph.reply(200, ok_message())
    result, code = main_send.handle_send_template(
        action(
            "send_template",
            {
                "template_name": "order_update",
                "language": "en_US",
                "body_parameters": ["Jessica", 12],
            },
        ),
        {},
    )
    assert code == 0, result
    assert graph.json_body() == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "37060000001",
        "type": "template",
        "template": {
            "name": "order_update",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Jessica"},
                        {"type": "text", "text": "12"},
                    ],
                }
            ],
        },
    }


def test_send_template_without_parameters_has_no_components(graph):
    graph.reply(200, ok_message())
    result, code = main_send.handle_send_template(
        action("send_template", {"template_name": "hello_world", "language": "en_US"}), {}
    )
    assert code == 0, result
    assert "components" not in graph.json_body()["template"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"language": "en"}, "template_name_required"),
        ({"template_name": "t"}, "language_required"),
        (
            {"template_name": "t", "language": "en", "body_parameters": "nope"},
            "template_parameters_invalid",
        ),
        (
            {"template_name": "t", "language": "en", "body_parameters": [{"a": 1}]},
            "template_parameters_invalid",
        ),
    ],
)
def test_send_template_input_errors(payload, expected):
    result, code = main_send.handle_send_template(action("send_template", payload), {})
    assert (code, result["error_code"]) == (2, expected)


def test_send_template_not_approved(graph):
    graph.reply(400, graph_error(132001))
    result, code = main_send.handle_send_template(
        action("send_template", {"template_name": "t", "language": "xx"}), {}
    )
    assert (code, result["error_code"]) == (2, "whatsapp_template_invalid")


# --- reaction and mark as read ------------------------------------------------------


def test_send_reaction(graph):
    graph.reply(200, ok_message())
    result, code = main_send.handle_send_reaction(
        action("send_reaction", {"message_id": "wamid.IN", "emoji": "\U0001f44d"}), {}
    )
    assert code == 0, result
    body = graph.json_body()
    assert body["type"] == "reaction"
    assert body["reaction"] == {"message_id": "wamid.IN", "emoji": "\U0001f44d"}
    assert "context" not in body  # a reaction cannot be a contextual reply


@pytest.mark.parametrize(
    ("payload", "expected"),
    [({"emoji": "x"}, "message_id_required"), ({"message_id": "w"}, "emoji_required")],
)
def test_send_reaction_input_errors(payload, expected):
    result, code = main_send.handle_send_reaction(action("send_reaction", payload), {})
    assert (code, result["error_code"]) == (2, expected)


def test_invalid_message_id_for_a_reaction_maps_131009(graph):
    graph.reply(400, graph_error(131009))
    result, code = main_send.handle_send_reaction(
        action("send_reaction", {"message_id": "bad", "emoji": "x"}), {}
    )
    assert (code, result["error_code"]) == (2, "whatsapp_parameter_invalid")


def test_mark_as_read_posts_the_status_body(graph):
    graph.reply(200, {"success": True})
    result, code = main_send.handle_mark_as_read(
        action("mark_as_read", {"message_id": "wamid.IN"}, contacts=[]), {}
    )
    assert code == 0, result
    assert result["marked_read"] is True
    assert graph.json_body() == {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": "wamid.IN",
    }
    assert graph.requests[0]["url"].endswith(f"/{PHONE_NUMBER_ID}/messages")


def test_mark_as_read_failures(graph):
    result, code = main_send.handle_mark_as_read(action("mark_as_read", {}), {})
    assert (code, result["error_code"]) == (2, "message_id_required")
    graph.reply(400, graph_error(131009))
    result, code = main_send.handle_mark_as_read(action("mark_as_read", {"message_id": "bad"}), {})
    assert (code, result["error_code"], result["marked_read"]) == (
        2,
        "whatsapp_parameter_invalid",
        False,
    )
    graph.reply(200, {"success": False})
    result, code = main_send.handle_mark_as_read(action("mark_as_read", {"message_id": "x"}), {})
    assert code == 2


# --- send_file -----------------------------------------------------------------------


def input_artifact(path: Path, *, size: int | None = None, name: str = "pic.png", ctype: str = ""):
    return {
        "inputs": [
            {
                "artifact_id": "art-in",
                "artifact_handle": "artifact:art-in",
                "binding_key": "source_artifact",
                "filename": name,
                **({"size_bytes": size} if size is not None else {}),
                **({"content_type": ctype} if ctype else {}),
                "access": {
                    "transport": "presigned_url",
                    "mode": "read",
                    "download_url": f"file://{path}",
                },
            }
        ]
    }


def output_artifact(path: Path):
    return {
        "outputs": [
            {
                "artifact_id": "art-out",
                "artifact_handle": "artifact:art-out",
                "binding_key": "downloaded_file",
                "access": {
                    "transport": "presigned_url",
                    "mode": "write",
                    "upload_url": f"file://{path}",
                },
            }
        ]
    }


def test_send_file_uploads_then_sends_an_image_with_caption(graph, tmp_path):
    source = tmp_path / "pic.png"
    source.write_bytes(b"PNGBYTES")
    graph.reply(200, {"id": "MEDIA9"})
    graph.reply(200, ok_message("wamid.F"))
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {
                "kind": "image",
                "artifact_handle": "artifact:art-in",
                "caption": "Look",
                "reply_to_message_id": "wamid.IN",
            },
            artifacts=input_artifact(source),
        ),
        {},
    )
    assert code == 0, result
    assert result["provider_message_id"] == "wamid.F"
    upload, send = graph.requests
    assert upload["url"].endswith(f"/{PHONE_NUMBER_ID}/media")
    assert b"PNGBYTES" in upload["body"] and b"image/png" in upload["body"]
    body = json.loads(send["body"])
    assert body["type"] == "image"
    assert body["image"] == {"id": "MEDIA9", "caption": "Look"}
    assert body["context"] == {"message_id": "wamid.IN"}


def test_send_file_document_carries_the_file_name(graph, tmp_path):
    source = tmp_path / "r.pdf"
    source.write_bytes(b"%PDF")
    graph.reply(200, {"id": "M"})
    graph.reply(200, ok_message())
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "document", "artifact_handle": "artifact:art-in", "file_name": "Report.pdf"},
            artifacts=input_artifact(source, name="r.pdf"),
        ),
        {},
    )
    assert code == 0, result
    assert graph.json_body()["document"] == {"id": "M", "filename": "Report.pdf"}


@pytest.mark.parametrize(
    ("kind", "name", "limit_mb"),
    [("image", "a.png", 5), ("audio", "a.mp3", 16), ("video", "a.mp4", 16)],
)
def test_send_file_enforces_the_size_limit_before_uploading(graph, tmp_path, kind, name, limit_mb):
    source = tmp_path / name
    source.write_bytes(b"x")
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": kind, "artifact_handle": "artifact:art-in"},
            artifacts=input_artifact(source, size=limit_mb * 1024 * 1024 + 1, name=name),
        ),
        {},
    )
    assert (code, result["error_code"]) == (2, "file_too_large")
    assert graph.requests == []


def test_send_file_document_limit_is_100_mb(graph, tmp_path):
    source = tmp_path / "a.pdf"
    source.write_bytes(b"x")
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "document", "artifact_handle": "artifact:art-in"},
            artifacts=input_artifact(source, size=100 * 1024 * 1024 + 1, name="a.pdf"),
        ),
        {},
    )
    assert (code, result["error_code"]) == (2, "file_too_large")
    assert graph.requests == []


def test_send_file_stops_a_stream_that_outgrows_the_limit(graph, tmp_path, monkeypatch):
    monkeypatch.setitem(main_steps.KIND_LIMITS_BYTES, "image", 4)
    source = tmp_path / "a.png"
    source.write_bytes(b"12345")
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "image", "artifact_handle": "artifact:art-in"},
            artifacts=input_artifact(source),
        ),
        {},
    )
    assert (code, result["error_code"]) == (2, "file_too_large")
    assert graph.requests == []


def test_send_file_rejects_a_mime_type_before_uploading(graph, tmp_path):
    source = tmp_path / "a.gif"
    source.write_bytes(b"GIF")
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "image", "artifact_handle": "artifact:art-in"},
            artifacts=input_artifact(source, name="a.gif"),
        ),
        {},
    )
    assert (code, result["error_code"]) == (2, "mime_type_unsupported")
    assert graph.requests == []


def test_send_file_mime_override_and_artifact_content_type(graph, tmp_path):
    source = tmp_path / "blob"
    source.write_bytes(b"data")
    graph.reply(200, {"id": "M"})
    graph.reply(200, ok_message())
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {
                "kind": "document",
                "artifact_handle": "artifact:art-in",
                "mime_type": "application/pdf",
            },
            artifacts=input_artifact(source, name="blob", ctype="application/octet-stream"),
        ),
        {},
    )
    assert code == 0, result
    assert b"application/pdf" in graph.requests[0]["body"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kind": "sticker", "artifact_handle": "a"}, "kind_invalid"),
        ({"artifact_handle": "a"}, "kind_invalid"),
        ({"kind": "image"}, "artifact_required"),
        ({"kind": "image", "artifact_handle": "a", "caption": "x" * 1025}, "caption_too_long"),
        ({"kind": "audio", "artifact_handle": "a", "caption": "x"}, "caption_not_supported"),
    ],
)
def test_send_file_input_errors(payload, expected):
    result, code = main_steps.handle_send_file(action("send_file", payload), {})
    assert (code, result["error_code"]) == (2, expected)


def test_send_file_caption_limit_boundary(graph, tmp_path):
    source = tmp_path / "a.png"
    source.write_bytes(b"x")
    graph.reply(200, {"id": "M"})
    graph.reply(200, ok_message())
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "image", "artifact_handle": "artifact:art-in", "caption": "c" * 1024},
            artifacts=input_artifact(source),
        ),
        {},
    )
    assert code == 0, result


def test_send_file_unreadable_and_empty_artifacts(graph, tmp_path):
    result, code = main_steps.handle_send_file(
        action("send_file", {"kind": "image", "artifact_handle": "artifact:nope"}), {}
    )
    assert (code, result["error_code"]) == (2, "artifact_read_failed")
    empty = tmp_path / "e.png"
    empty.write_bytes(b"")
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "image", "artifact_handle": "artifact:art-in"},
            artifacts=input_artifact(empty),
        ),
        {},
    )
    assert (code, result["error_code"]) == (2, "artifact_empty")
    assert graph.requests == []


def test_send_file_upload_rejection_is_mapped_and_nothing_is_sent(graph, tmp_path):
    source = tmp_path / "a.png"
    source.write_bytes(b"x")
    graph.reply(400, graph_error(131053, "bad media"))
    result, code = main_steps.handle_send_file(
        action(
            "send_file",
            {"kind": "image", "artifact_handle": "artifact:art-in"},
            artifacts=input_artifact(source),
        ),
        {},
    )
    assert (code, result["error_code"]) == (2, "whatsapp_media_rejected")
    assert len(graph.requests) == 1


# --- get_file --------------------------------------------------------------------------


def test_get_file_downloads_into_an_artifact(graph, tmp_path):
    target = tmp_path / "out.bin"
    graph.reply(
        200,
        {"url": MEDIA_URL, "mime_type": "image/jpeg", "file_size": 10, "sha256": "x", "id": "M1"},
    )
    graph.reply(200, b"jpeg bytes", {"content-type": "image/jpeg"})
    result, code = main_steps.handle_get_file(
        action("get_file", {"file_id": "M1"}, artifacts=output_artifact(target)), {}
    )
    assert code == 0, result
    assert target.read_bytes() == b"jpeg bytes"
    assert result["artifact_handle"] == "artifact:art-out"
    assert result["artifact_filename"] == "whatsapp_M1.jpg"
    assert result["mime_type"] == "image/jpeg"
    assert result["size_bytes"] == len(b"jpeg bytes")
    assert len(result["sha256"]) == 64
    info, download = graph.requests
    assert info["url"].startswith("https://graph.facebook.com/v26.0/M1")
    assert download["url"] == MEDIA_URL
    assert download["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(result)


def test_get_file_uses_a_given_file_name(graph, tmp_path):
    target = tmp_path / "out.bin"
    graph.reply(200, {"url": MEDIA_URL, "mime_type": "application/pdf", "file_size": 4})
    graph.reply(200, b"%PDF")
    result, code = main_steps.handle_get_file(
        action(
            "get_file",
            {"file_id": "M2", "file_name": "invoice.pdf"},
            artifacts=output_artifact(target),
        ),
        {},
    )
    assert code == 0, result
    assert result["artifact_filename"] == "invoice.pdf"


def test_get_file_refuses_a_declared_size_over_100_mb(graph, tmp_path):
    target = tmp_path / "out.bin"
    graph.reply(200, {"url": MEDIA_URL, "mime_type": "x/y", "file_size": 100 * 1024 * 1024 + 1})
    result, code = main_steps.handle_get_file(
        action("get_file", {"file_id": "big"}, artifacts=output_artifact(target)), {}
    )
    assert (code, result["error_code"]) == (2, "file_too_large")
    assert not target.exists()
    assert len(graph.requests) == 1


def test_get_file_does_not_download_from_a_foreign_host(graph, tmp_path):
    target = tmp_path / "out.bin"
    graph.reply(200, {"url": "https://evil.example.com/x", "mime_type": "x/y", "file_size": 1})
    result, code = main_steps.handle_get_file(
        action("get_file", {"file_id": "M"}, artifacts=output_artifact(target)), {}
    )
    assert (code, result["error_code"]) == (2, "whatsapp_media_url_rejected")
    assert len(graph.requests) == 1
    assert not target.exists()


def test_get_file_reports_an_unknown_media_id(graph, tmp_path):
    target = tmp_path / "out.bin"
    graph.reply(400, graph_error(100, "Unsupported get request"))
    result, code = main_steps.handle_get_file(
        action("get_file", {"file_id": "nope"}, artifacts=output_artifact(target)), {}
    )
    assert (code, result["error_code"]) == (2, "whatsapp_parameter_invalid")


def test_get_file_expired_link_says_run_again(graph, tmp_path):
    target = tmp_path / "out.bin"
    graph.reply(200, {"url": MEDIA_URL, "mime_type": "x/y", "file_size": 1})
    graph.reply(404, b"")
    result, code = main_steps.handle_get_file(
        action("get_file", {"file_id": "M"}, artifacts=output_artifact(target)), {}
    )
    assert (code, result["error_code"]) == (2, "whatsapp_media_not_found")


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        (action("get_file", {}), "file_id_required"),
        (action("get_file", {"file_id": "M"}, token=""), "access_token_required"),
    ],
)
def test_get_file_input_errors(built, expected):
    result, code = main_steps.handle_get_file(built, {})
    assert (code, result["error_code"]) == (2, expected)
