"""The single transport module: Graph error mapping, redaction, ambiguity and media URLs."""

from __future__ import annotations

import json
import urllib.error

import pytest
from conftest import MEDIA_URL, PHONE_NUMBER_ID, TOKEN, graph_error, ok_message

import main_transport as t

SETTINGS = {"phone_number_id": PHONE_NUMBER_ID, "graph_api_version": "v26.0"}


@pytest.mark.parametrize(
    ("graph_code", "expected", "retryable"),
    [
        (131047, "whatsapp_window_closed", False),
        (131009, "whatsapp_parameter_invalid", False),
        (131008, "whatsapp_parameter_missing", False),
        (100, "whatsapp_parameter_invalid", False),
        (131053, "whatsapp_media_rejected", False),
        (190, "whatsapp_token_invalid", False),
        (4, "whatsapp_rate_limited", True),
        (80007, "whatsapp_rate_limited", True),
        (130429, "whatsapp_rate_limited", True),
        (131056, "whatsapp_rate_limited", True),
        (131026, "whatsapp_undeliverable", False),
        (132000, "whatsapp_template_invalid", False),
        (132001, "whatsapp_template_invalid", False),
        (132005, "whatsapp_template_invalid", False),
        (132012, "whatsapp_template_invalid", False),
        (133010, "whatsapp_number_not_registered", False),
        (131021, "whatsapp_recipient_is_sender", False),
        (368, "whatsapp_account_restricted", False),
    ],
)
def test_every_documented_graph_code_maps_to_a_stable_code(graph, graph_code, expected, retryable):
    graph.reply(400, graph_error(graph_code))
    response = t.send_graph_message(
        TOKEN, SETTINGS, {"to": "1", "type": "text", "text": {"body": "x"}}
    )
    assert response["ok"] is False
    assert response["error_code"] == expected
    assert response["retryable"] is retryable
    assert response["definitely_no_external_effect"] is True
    assert response["graph_error_code"] == graph_code


def test_outside_the_24_hour_window_tells_the_user_what_to_do(graph):
    graph.reply(400, graph_error(131047, "Re-engagement message"))
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert response["error_code"] == "whatsapp_window_closed"
    assert "send template" in response["error"].lower()
    assert "write first" in response["error"].lower()


def test_error_details_are_appended_to_the_message(graph):
    graph.reply(400, graph_error(131053, "x", error_data={"details": "mime type mismatch"}))
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "image"})
    assert "mime type mismatch" in response["error"]


def test_an_unknown_4xx_code_is_a_definite_rejection(graph):
    graph.reply(400, graph_error(999999, "Something odd"))
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert response["error_code"] == "whatsapp_request_rejected"
    assert response["definitely_no_external_effect"] is True
    assert "Something odd" in response["error"]


def test_a_5xx_answer_is_ambiguous_and_never_retryable(graph):
    graph.reply(503, {"error": {"message": "down", "code": 2}})
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert response["error_code"] == "whatsapp_send_ambiguous"
    assert response["definitely_no_external_effect"] is False
    assert response["external_effect_status"] == "timeout_unknown"
    assert response["retryable"] is False


def test_a_timeout_is_ambiguous_for_a_send_and_retryable_for_a_read(graph):
    graph.fail(TimeoutError("slow"))
    sent = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert sent["error_code"] == "whatsapp_send_ambiguous"
    graph.fail(TimeoutError("slow"))
    read = t.get_phone_number(TOKEN, SETTINGS)
    assert read["error_code"] == "whatsapp_unreachable"
    assert read["retryable"] is True
    assert read["definitely_no_external_effect"] is True


def test_success_without_a_message_id_is_ambiguous(graph):
    graph.reply(200, {"messages": []})
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert response["error_code"] == "whatsapp_send_ambiguous"


def test_the_token_is_only_in_the_authorization_header(graph):
    graph.reply(200, ok_message())
    response = t.send_graph_message(
        TOKEN, SETTINGS, {"type": "text", "to": "1", "text": {"body": "x"}}
    )
    assert response == {"ok": True, "message_id": "wamid.OUT1"}
    request = graph.requests[0]
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in request["url"]
    assert TOKEN.encode() not in request["body"]
    assert request["url"] == f"https://graph.facebook.com/v26.0/{PHONE_NUMBER_ID}/messages"
    body = json.loads(request["body"])
    assert body["messaging_product"] == "whatsapp"


def test_a_token_in_a_graph_message_is_redacted_everywhere(graph):
    graph.reply(
        401,
        graph_error(190, f"Invalid OAuth access token {TOKEN} Bearer {TOKEN} access_token={TOKEN}"),
    )
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert TOKEN not in json.dumps(response)
    assert response["error_code"] == "whatsapp_token_invalid"


def test_an_exception_text_never_carries_the_token(graph):
    graph.fail(urllib.error.URLError(f"failed with Bearer {TOKEN}"))
    response = t.send_graph_message(TOKEN, SETTINGS, {"type": "text"})
    assert TOKEN not in json.dumps(response)


def test_redact_helper():
    assert t.redact(f"x {TOKEN} y", TOKEN) == "x <redacted> y"
    assert "abc123" not in t.redact("Authorization: Bearer abc123")
    assert "abc123" not in t.redact("https://x/?access_token=abc123&y=1")


def test_missing_token_and_phone_number_id_are_refused_before_any_request(graph):
    assert t.send_graph_message("", SETTINGS, {})["error_code"] == "access_token_required"
    assert (
        t.send_graph_message(TOKEN, {"phone_number_id": ""}, {})["error_code"]
        == "phone_number_id_required"
    )
    assert graph.requests == []


def test_upload_is_multipart_with_the_mime_type(graph):
    graph.reply(200, {"id": "MEDIA1"})
    response = t.upload_media(
        TOKEN, SETTINGS, filename="a.png", mime_type="image/png", data=b"PNGDATA"
    )
    assert response == {"ok": True, "media_id": "MEDIA1"}
    request = graph.requests[0]
    assert request["url"].endswith(f"/{PHONE_NUMBER_ID}/media")
    assert request["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="messaging_product"' in request["body"]
    assert b"whatsapp" in request["body"]
    assert b'name="type"' in request["body"]
    assert b"image/png" in request["body"]
    assert b"PNGDATA" in request["body"]


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        (MEDIA_URL, True),
        ("https://graph.facebook.com/v26.0/x", True),
        ("https://scontent.xx.fbcdn.net/file", True),
        ("http://lookaside.fbsbx.com/x", False),
        ("https://evil.example.com/x", False),
        ("https://lookaside.fbsbx.com.evil.com/x", False),
        ("https://user:pw@lookaside.fbsbx.com/x", False),
        ("file:///etc/passwd", False),
    ],
)
def test_the_bearer_token_goes_only_to_meta_hosts(url, allowed):
    assert t.media_url_allowed(url) is allowed


def test_download_sends_the_bearer_and_returns_bytes(graph):
    graph.reply(200, b"BYTES", {"content-type": "image/jpeg"})
    response = t.download_media(TOKEN, MEDIA_URL, max_bytes=100)
    assert response == {"ok": True, "data": b"BYTES", "content_type": "image/jpeg"}
    assert graph.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_download_refuses_a_foreign_host_without_a_request(graph):
    response = t.download_media(TOKEN, "https://evil.example.com/x", max_bytes=100)
    assert response["error_code"] == "whatsapp_media_url_rejected"
    assert graph.requests == []


def test_download_follows_a_redirect_only_to_a_meta_media_host(graph):
    graph.reply(302, b"", {"location": "https://scontent.xx.fbcdn.net/v/media.jpg"})
    graph.reply(200, b"BYTES", {"content-type": "image/jpeg"})
    response = t.download_media(TOKEN, MEDIA_URL, max_bytes=100)
    assert response["ok"] is True
    assert graph.requests[1]["url"] == "https://scontent.xx.fbcdn.net/v/media.jpg"


def test_download_refuses_a_redirect_off_meta_and_keeps_the_token_home(graph):
    graph.reply(302, b"", {"location": "http://collector.example.invalid/steal"})
    response = t.download_media(TOKEN, MEDIA_URL, max_bytes=100)
    assert response["error_code"] == "whatsapp_media_url_rejected"
    assert len(graph.requests) == 1


def test_urllib_does_not_follow_redirects_itself():
    assert t._NO_REDIRECT_OPENER.handle_open  # built with the no-redirect handler
    handler = t._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://x.invalid/") is None


def test_download_404_says_the_link_expired(graph):
    graph.reply(404, b"")
    response = t.download_media(TOKEN, MEDIA_URL, max_bytes=100)
    assert response["error_code"] == "whatsapp_media_not_found"
    assert "5 minutes" in response["error"]


def test_download_over_the_limit_is_refused_not_cut(graph):
    graph.reply(200, b"x" * 11)
    response = t.download_media(TOKEN, MEDIA_URL, max_bytes=10)
    assert response["error_code"] == "file_too_large"


def test_media_info(graph):
    graph.reply(
        200,
        {"url": MEDIA_URL, "mime_type": "image/jpeg", "sha256": "ab", "file_size": 12, "id": "M"},
    )
    info = t.get_media_info(TOKEN, SETTINGS, "M")
    assert info["url"] == MEDIA_URL
    assert info["file_size"] == 12
    assert graph.requests[0]["method"] == "GET"
    assert f"phone_number_id={PHONE_NUMBER_ID}" in graph.requests[0]["url"]


def test_get_phone_number_reads_the_number(graph):
    graph.reply(200, {"verified_name": "Shop", "display_phone_number": "+370 600 00000", "id": "1"})
    response = t.get_phone_number(TOKEN, SETTINGS)
    assert response["data"]["verified_name"] == "Shop"
    assert "fields=verified_name" in graph.requests[0]["url"]
