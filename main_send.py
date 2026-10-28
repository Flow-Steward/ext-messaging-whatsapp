"""Workflow steps that send: message, template, reaction and mark-as-read.

A step is bound to a WhatsApp connection (phone number ID, access token, contacts).
A message goes to one of the connection's contacts, chosen by its ID (``channel_id``) or by
its phone number (``wa_id``); a project with a single contact enabled for notifications may
leave both out. Every limit is checked here, before anything is sent, and a message that
is too long is refused with a clear error code and never shortened.

Test Run never calls WhatsApp: the step reports ``external_effect_status: suppressed``.
"""

from __future__ import annotations

import json
from typing import Any

from main_state import (
    _as_dict,
    _as_str,
    _contact_items_from_config,
    _coerce_bool,
    connection_settings,
    normalize_wa_id,
)
from main_transport import graph_call, send_graph_message

WHATSAPP_TEXT_LIMIT = 4096
WHATSAPP_BUTTON_BODY_LIMIT = 1024
WHATSAPP_BUTTON_TITLE_LIMIT = 20
WHATSAPP_BUTTON_ID_LIMIT = 256
WHATSAPP_MAX_BUTTONS = 3


def _failure(code: str, message: str) -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": False,
            "delivered": False,
            "error_code": code,
            "error": message,
            "external_effect_status": "failed",
            "definitely_no_external_effect": True,
            "provider_message_id": "",
        },
        2,
    )


def _test_mode(action: dict[str, Any], runtime_context: dict[str, Any]) -> bool:
    context = _as_dict(action.get("context"))
    return _coerce_bool(runtime_context.get("test_mode"), False) or _coerce_bool(
        context.get("test_mode"), False
    )


def _suppressed(channel_id: str = "") -> tuple[dict[str, Any], int]:
    return (
        {
            "ok": True,
            "delivered": False,
            "external_effect_status": "suppressed",
            "provider_message_id": "",
            "channel_id": channel_id,
        },
        0,
    )


def _connection(action: dict[str, Any]) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """``(connection, access token, normalized settings)`` of the step's target."""
    connection = _as_dict(_as_dict(action.get("target")).get("connection"))
    config = _as_dict(connection.get("connection_config") or connection.get("config"))
    token = _as_str(_as_dict(connection.get("secrets")).get("access_token"))
    return connection, token, {**config, **connection_settings(config)}


def _notify_contacts(contacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        c
        for c in contacts
        if _coerce_bool(c.get("enabled"), True)
        and _coerce_bool(_as_dict(c.get("capabilities")).get("notify"), False)
        and _as_str(c.get("wa_id"))
    ]


def select_contact(
    contacts: list[dict[str, Any]], channel_id: str, wa_id: str = ""
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """The contact to send to, or the failure that explains why there is none."""
    usable = _notify_contacts(contacts)
    wanted_wa_id = normalize_wa_id(wa_id) if wa_id else ""
    if wa_id and not wanted_wa_id:
        return None, _failure("wa_id_invalid", "wa_id must be a phone number of 7 to 15 digits.")
    if not channel_id and not wanted_wa_id:
        if len(usable) == 1:
            return usable[0], None
        if not usable:
            return None, _failure(
                "no_notify_contact",
                "This WhatsApp connection has no contact enabled for notifications. "
                "Add one on the Contacts page.",
            )
        ids = ", ".join(_as_str(c.get("channel_id")) for c in usable)
        return None, _failure(
            "channel_id_required",
            f"This connection has several contacts; choose one by its ID: {ids}.",
        )
    if channel_id:
        contact = next((c for c in contacts if _as_str(c.get("channel_id")) == channel_id), None)
        if contact is None:
            ids = ", ".join(_as_str(c.get("channel_id")) for c in contacts) or "none"
            return None, _failure(
                "unknown_channel_id",
                f"No contact '{channel_id}' on this WhatsApp connection. Contact IDs: {ids}.",
            )
    else:
        contact = next((c for c in contacts if _as_str(c.get("wa_id")) == wanted_wa_id), None)
        if contact is None:
            return None, _failure(
                "unknown_wa_id",
                f"No saved contact has the phone number {wanted_wa_id}. Save it on the "
                "Contacts page first.",
            )
    name = _as_str(contact.get("channel_id"))
    if not _coerce_bool(contact.get("enabled"), True):
        return None, _failure("channel_disabled", f"Contact '{name}' is disabled.")
    if not _coerce_bool(_as_dict(contact.get("capabilities")).get("notify"), False):
        return None, _failure(
            "channel_missing_notify", f"Contact '{name}' is not allowed to receive notifications."
        )
    if not _as_str(contact.get("wa_id")):
        return None, _failure(
            "contact_wa_id_missing", f"Contact '{name}' has no valid phone number."
        )
    return contact, None


def resolve_contact(
    action: dict[str, Any], input_payload: dict[str, Any]
) -> tuple[tuple[str, dict[str, Any], dict[str, Any]] | None, tuple[dict[str, Any], int] | None]:
    """``((token, contact, settings), None)`` or ``(None, failure)``."""
    connection, token, settings = _connection(action)
    if not connection:
        return None, _failure(
            "project_connection_required", "Choose the WhatsApp connection this step uses."
        )
    contacts = [c for c in _contact_items_from_config(settings) if isinstance(c, dict)]
    contact, failure = select_contact(
        contacts, _as_str(input_payload.get("channel_id")), _as_str(input_payload.get("wa_id"))
    )
    if failure is not None or contact is None:
        return None, failure or _failure("channel_id_required", "Choose a contact by its ID.")
    return (token, contact, settings), None


def step_result(response: dict[str, Any], contact: dict[str, Any]) -> tuple[dict[str, Any], int]:
    delivered = bool(response.get("ok"))
    message_id = _as_str(response.get("message_id"))
    result: dict[str, Any] = {
        "ok": delivered,
        "delivered": delivered,
        "provider_message_id": message_id,
        "channel_id": _as_str(contact.get("channel_id")),
        "external_effect_status": "succeeded" if delivered else "failed",
    }
    if not delivered:
        result["error"] = _as_str(response.get("error"))
        result["error_code"] = _as_str(response.get("error_code")) or "whatsapp_request_failed"
    for field in (
        "external_effect_status",
        "definitely_no_external_effect",
        "retryable",
        "failure_class",
        "graph_error_code",
    ):
        if field in response:
            result[field] = response[field]
    return result, 0 if delivered else 2


def _wamid(value: Any) -> str:
    return _as_str(value)


def _recipient(contact: dict[str, Any]) -> dict[str, Any]:
    return {"recipient_type": "individual", "to": _as_str(contact.get("wa_id"))}


def _context(input_payload: dict[str, Any]) -> dict[str, Any]:
    reply_to = _wamid(input_payload.get("reply_to_message_id"))
    return {"context": {"message_id": reply_to}} if reply_to else {}


def parse_buttons(
    value: Any,
) -> tuple[list[dict[str, Any]] | None, tuple[dict[str, Any], int] | None]:
    """The WhatsApp reply-button list for ``[{id, title}, ...]``, or the failure why not.

    At most 3 buttons; a title is at most 20 characters and unique; an id is at most 256
    characters and unique. A JSON string of the list is accepted, as a workflow variable
    often arrives as text. Empty or absent means no buttons.
    """
    if value is None or value == "" or value == []:
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, _failure("buttons_invalid", "The buttons are not valid JSON.")
    if not isinstance(value, list):
        return None, _failure("buttons_invalid", "Buttons must be a list of {id, title} objects.")
    if len(value) > WHATSAPP_MAX_BUTTONS:
        return None, _failure(
            "buttons_invalid",
            f"WhatsApp allows at most {WHATSAPP_MAX_BUTTONS} reply buttons; got {len(value)}.",
        )
    buttons: list[dict[str, Any]] = []
    ids: set[str] = set()
    titles: set[str] = set()
    for raw in value:
        item = _as_dict(raw)
        button_id = _as_str(item.get("id"))
        title = _as_str(item.get("title"))
        if not button_id or not title:
            return None, _failure("buttons_invalid", "Every button needs an id and a title.")
        if len(title) > WHATSAPP_BUTTON_TITLE_LIMIT:
            return None, _failure(
                "buttons_invalid",
                f"The title of button '{title[:25]}' has {len(title)} characters; "
                f"WhatsApp allows at most {WHATSAPP_BUTTON_TITLE_LIMIT}.",
            )
        if len(button_id) > WHATSAPP_BUTTON_ID_LIMIT:
            return None, _failure(
                "buttons_invalid",
                f"A button id is longer than {WHATSAPP_BUTTON_ID_LIMIT} characters.",
            )
        if button_id in ids or title in titles:
            return None, _failure("buttons_invalid", "Button ids and titles must be unique.")
        ids.add(button_id)
        titles.add(title)
        buttons.append({"type": "reply", "reply": {"id": button_id, "title": title}})
    return buttons, None


def handle_send_message(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run the ``send_message`` workflow operation."""
    input_payload = _as_dict(action.get("input"))
    text = input_payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return _failure("text_required", "Map the message text before running this step.")
    buttons, failure = parse_buttons(input_payload.get("buttons"))
    if failure is not None:
        return failure
    limit = WHATSAPP_BUTTON_BODY_LIMIT if buttons else WHATSAPP_TEXT_LIMIT
    if len(text) > limit:
        if buttons:
            return _failure(
                "message_too_long_for_buttons",
                f"A message with reply buttons holds at most {WHATSAPP_BUTTON_BODY_LIMIT} "
                f"characters; this one has {len(text)}. It is not shortened.",
            )
        return _failure(
            "message_too_long",
            f"WhatsApp accepts at most {WHATSAPP_TEXT_LIMIT} characters per message; "
            f"this one has {len(text)}. It is not shortened.",
        )
    if _test_mode(action, runtime_context):
        return _suppressed(_as_str(input_payload.get("channel_id")))
    resolved, failure = resolve_contact(action, input_payload)
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a contact by its ID.")
    token, contact, settings = resolved
    payload: dict[str, Any] = {**_recipient(contact), **_context(input_payload)}
    if buttons:
        payload["type"] = "interactive"
        payload["interactive"] = {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": buttons},
        }
    else:
        payload["type"] = "text"
        payload["text"] = {"body": text}
        if _coerce_bool(input_payload.get("preview_url"), False):
            payload["text"]["preview_url"] = True
    return step_result(send_graph_message(token, settings, payload), contact)


def _string_list(value: Any) -> tuple[list[str] | None, bool]:
    """``(items, ok)`` for a list of strings or a JSON string of one."""
    if value is None or value == "" or value == []:
        return [], True
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None, False
    if not isinstance(value, list):
        return None, False
    items: list[str] = []
    for item in value:
        if isinstance(item, (dict, list)) or item is None:
            return None, False
        items.append(str(item))
    return items, True


def handle_send_template(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``send_template``: an approved template with positional body text parameters.

    Header and button parameters are not supported: Meta's template overview page does not
    document their shape (docs/DECISIONS.md D-7).
    """
    input_payload = _as_dict(action.get("input"))
    name = _as_str(input_payload.get("template_name"))
    if not name:
        return _failure("template_name_required", "Map the name of an approved template.")
    language = _as_str(input_payload.get("language"))
    if not language:
        return _failure(
            "language_required", "Map the template language code, for example en_US or lt."
        )
    parameters, ok = _string_list(input_payload.get("body_parameters"))
    if not ok or parameters is None:
        return _failure(
            "template_parameters_invalid",
            'body_parameters must be a list of texts, for example ["Jessica", "12"].',
        )
    if _test_mode(action, runtime_context):
        return _suppressed(_as_str(input_payload.get("channel_id")))
    resolved, failure = resolve_contact(action, input_payload)
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a contact by its ID.")
    token, contact, settings = resolved
    template: dict[str, Any] = {"name": name, "language": {"code": language}}
    if parameters:
        template["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in parameters]}
        ]
    payload = {**_recipient(contact), "type": "template", "template": template}
    return step_result(send_graph_message(token, settings, payload), contact)


def handle_send_reaction(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``send_reaction``: react with an emoji to a message the contact sent."""
    input_payload = _as_dict(action.get("input"))
    message_id = _wamid(input_payload.get("message_id"))
    if not message_id:
        return _failure(
            "message_id_required", "Map the ID of the received message to react to (a wamid)."
        )
    emoji = _as_str(input_payload.get("emoji"))
    if not emoji:
        return _failure("emoji_required", "Map the emoji to react with.")
    if _test_mode(action, runtime_context):
        return _suppressed(_as_str(input_payload.get("channel_id")))
    resolved, failure = resolve_contact(action, input_payload)
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a contact by its ID.")
    token, contact, settings = resolved
    payload = {
        **_recipient(contact),
        "type": "reaction",
        "reaction": {"message_id": message_id, "emoji": emoji},
    }
    return step_result(send_graph_message(token, settings, payload), contact)


def handle_mark_as_read(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``mark_as_read``: show the blue ticks for a received message."""
    input_payload = _as_dict(action.get("input"))
    message_id = _wamid(input_payload.get("message_id"))
    if not message_id:
        return _failure(
            "message_id_required", "Map the ID of the received message to mark as read (a wamid)."
        )
    if _test_mode(action, runtime_context):
        return (
            {
                "ok": True,
                "marked_read": False,
                "message_id": message_id,
                "external_effect_status": "suppressed",
            },
            0,
        )
    connection, token, settings = _connection(action)
    if not connection:
        return _failure(
            "project_connection_required", "Choose the WhatsApp connection this step uses."
        )
    phone_number_id = _as_str(settings.get("phone_number_id"))
    if not phone_number_id:
        return _failure(
            "phone_number_id_required", "Save the phone number ID on the Setup page first."
        )
    response = graph_call(
        token,
        _as_str(settings.get("graph_api_version")),
        "POST",
        f"{phone_number_id}/messages",
        json_body={"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
    )
    ok = bool(response.get("ok")) and _coerce_bool(
        _as_dict(response.get("data")).get("success"), False
    )
    result: dict[str, Any] = {
        "ok": ok,
        "marked_read": ok,
        "message_id": message_id,
        "external_effect_status": "succeeded" if ok else "failed",
    }
    if not ok:
        failed = response if not response.get("ok") else {}
        result["error_code"] = _as_str(failed.get("error_code")) or "whatsapp_request_failed"
        result["error"] = _as_str(failed.get("error")) or "WhatsApp did not confirm the read mark."
        for field in ("definitely_no_external_effect", "retryable", "external_effect_status"):
            if field in failed:
                result[field] = failed[field]
    return result, 0 if ok else 2


__all__ = [
    "WHATSAPP_TEXT_LIMIT",
    "handle_mark_as_read",
    "handle_send_message",
    "handle_send_reaction",
    "handle_send_template",
    "parse_buttons",
    "resolve_contact",
    "select_contact",
    "step_result",
]
