"""The one place that talks to the WhatsApp Business Cloud API (the Graph API).

Every network call of the extension goes through :func:`_http_exchange`; tests
replace that single function and never touch the network. Above it,
:func:`graph_call` builds the request, parses the answer and maps a Graph error
to a stable extension error code (:func:`map_graph_error`).

The access token travels only in the ``Authorization`` header. It is never put in
a URL, a log line, an error or a result: every text that leaves this module goes
through :func:`redact`.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib import error as urlerror
from urllib import parse
from urllib import request as urlrequest

from main_state import APPLICATION_JSON_VALUE, _as_dict, _as_str, _coerce_int

GRAPH_HOST = "graph.facebook.com"
GRAPH_BASE_URL = f"https://{GRAPH_HOST}"
# Hosts a media download URL may point to. Facts file: Meta documents no host for it, so
# the bearer token is only ever sent to hosts on this list (see docs/DECISIONS.md D-9).
MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".facebook.com", ".fbcdn.net")
JSON_TIMEOUT_SECONDS = 15
UPLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_TIMEOUT_SECONDS = 60

_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-~+/=]+")
_TOKEN_PARAM_RE = re.compile(r"(?i)(access_token=)[^&\s\"']+")


def redact(value: Any, *secrets: str) -> str:
    """``value`` as text with the given secrets and any bearer / access_token text removed."""
    text = _as_str(value)
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "<redacted>")
    text = _BEARER_RE.sub(r"\1<redacted>", text)
    return _TOKEN_PARAM_RE.sub(r"\1<redacted>", text)


# --- the single network function ------------------------------------------------


class ResponseTooLarge(Exception):
    """The answer passed the byte limit; nothing is truncated, the call fails."""


def _http_exchange(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
    max_bytes: int,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTPS request: ``(status, response headers, body)``.

    An HTTP error status is returned, not raised; a connection problem or a timeout
    raises. The body is read up to ``max_bytes``; more raises :class:`ResponseTooLarge`.
    """
    if not url.startswith("https://"):
        raise ValueError("Only https URLs are allowed")
    request = urlrequest.Request(url, data=body, headers=headers, method=method)
    try:
        with urlrequest.urlopen(request, timeout=timeout) as response:  # nosec B310 - https only, host checked by the caller
            status = int(getattr(response, "status", 200) or 200)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            data = response.read(max_bytes + 1)
    except urlerror.HTTPError as exc:
        status = int(exc.code or 0)
        response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        data = exc.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ResponseTooLarge(f"The answer is larger than {max_bytes} bytes")
    return status, response_headers, data


# --- error mapping ---------------------------------------------------------------

# Graph error code -> (extension error code, plain message, retryable).
# Each code's meaning was checked against Meta's error-code reference (docs/DECISIONS.md D-6).
GRAPH_ERRORS: dict[int, tuple[str, str, bool]] = {
    131047: (
        "whatsapp_window_closed",
        "WhatsApp only lets you send free-form messages within 24 hours after the person "
        "last wrote to this number. Use the Send template step, or wait for the person to "
        "write first.",
        False,
    ),
    131009: (
        "whatsapp_parameter_invalid",
        "WhatsApp rejected a parameter value as invalid (for example an unknown message ID).",
        False,
    ),
    131008: ("whatsapp_parameter_missing", "WhatsApp says a required parameter is missing.", False),
    100: (
        "whatsapp_parameter_invalid",
        "WhatsApp rejected the request: an unsupported or misspelled parameter, or a value "
        "that is too long.",
        False,
    ),
    131053: (
        "whatsapp_media_rejected",
        "WhatsApp could not accept the media, for example because its file type is not "
        "supported. Check the file type.",
        False,
    ),
    131052: ("whatsapp_media_download_failed", "WhatsApp could not download the media.", False),
    131051: (
        "whatsapp_unsupported_message_type",
        "WhatsApp does not support this message type.",
        False,
    ),
    190: (
        "whatsapp_token_invalid",
        "WhatsApp rejected the access token: it has expired or is no longer valid. "
        "Save a new token on the Setup page.",
        False,
    ),
    4: (
        "whatsapp_rate_limited",
        "The app reached its WhatsApp API call limit. Try again later.",
        True,
    ),
    80007: (
        "whatsapp_rate_limited",
        "The WhatsApp Business Account reached its rate limit. Try again later.",
        True,
    ),
    130429: (
        "whatsapp_rate_limited",
        "The WhatsApp message throughput limit was reached. Try again later.",
        True,
    ),
    131056: (
        "whatsapp_rate_limited",
        "Too many messages to the same person in a short time. Wait and try again.",
        True,
    ),
    131026: (
        "whatsapp_undeliverable",
        "WhatsApp cannot deliver this message: the number may not be on WhatsApp, the person "
        "has not accepted the latest terms, or their WhatsApp is outdated.",
        False,
    ),
    131021: (
        "whatsapp_recipient_is_sender",
        "The recipient is the sending number itself.",
        False,
    ),
    133010: (
        "whatsapp_number_not_registered",
        "The phone number is not registered on the WhatsApp Business Platform.",
        False,
    ),
    132000: (
        "whatsapp_template_invalid",
        "The number of template parameters does not match the template.",
        False,
    ),
    132001: (
        "whatsapp_template_invalid",
        "The template does not exist in this language, or it is not approved.",
        False,
    ),
    132005: ("whatsapp_template_invalid", "The translated template text is too long.", False),
    132012: (
        "whatsapp_template_invalid",
        "A template parameter is not in the format the template expects.",
        False,
    ),
    368: (
        "whatsapp_account_restricted",
        "The WhatsApp Business Account is restricted or disabled for a policy violation.",
        False,
    ),
    131042: ("whatsapp_payment_issue", "WhatsApp reports a billing or payment problem.", False),
    131016: (
        "whatsapp_service_unavailable",
        "WhatsApp is temporarily unavailable. Try again.",
        False,
    ),
    131000: ("whatsapp_request_failed", "WhatsApp failed with an unknown error. Try again.", False),
}


def _failure(
    code: str,
    message: str,
    *,
    definite: bool,
    retryable: bool = False,
    status: int = 0,
    graph_code: int | None = None,
    subcode: int | None = None,
    details: str = "",
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": False,
        "error": message,
        "error_code": code,
        "external_effect_status": "failed" if definite else "timeout_unknown",
        "definitely_no_external_effect": definite,
        "failure_class": "provider" if definite else "transient",
        "retryable": bool(retryable and definite),
    }
    if status:
        response["http_status"] = status
    if graph_code is not None:
        response["graph_error_code"] = graph_code
    if subcode is not None:
        response["graph_error_subcode"] = subcode
    if details:
        response["error_details"] = details
    return response


def ambiguous_failure(error: Any, *, token: str = "") -> dict[str, Any]:
    """The outcome is unknown (timeout, connection loss, 5xx): never retried blindly."""
    return _failure(
        "whatsapp_send_ambiguous",
        "WhatsApp may have accepted the request; check the chat or delivery history before "
        f"retrying. ({redact(error, token)})",
        definite=False,
    )


def map_graph_error(status: int, parsed: dict[str, Any], *, token: str = "") -> dict[str, Any]:
    """The failure for a Graph answer ``{error: {message, type, code, error_subcode, ...}}``."""
    error = _as_dict(parsed.get("error"))
    code = _coerce_int(error.get("code"))
    subcode = _coerce_int(error.get("error_subcode"))
    message = redact(error.get("message") or error.get("error_user_msg") or "", token)
    details = redact(_as_dict(error.get("error_data")).get("details") or "", token)
    mapped = GRAPH_ERRORS.get(code) if code is not None else None
    if mapped is not None:
        ext_code, text, retryable = mapped
        if details:
            text = f"{text} ({details})"
        return _failure(
            ext_code,
            text,
            definite=True,
            retryable=retryable,
            status=status,
            graph_code=code,
            subcode=subcode,
            details=details,
        )
    if status >= 500 or status == 0:
        return ambiguous_failure(message or f"HTTP {status}", token=token)
    text = f"WhatsApp rejected the request: {message or f'HTTP {status}'}"
    return _failure(
        "whatsapp_request_rejected",
        text,
        definite=True,
        status=status,
        graph_code=code,
        subcode=subcode,
        details=details,
    )


def _parse_json(raw: bytes) -> dict[str, Any]:
    try:
        return _as_dict(json.loads(raw.decode("utf-8", errors="replace"))) if raw else {}
    except ValueError:
        return {}


# --- Graph calls --------------------------------------------------------------------


def _multipart_body(
    fields: dict[str, str], *, file_field: str, filename: str, content_type: str, data: bytes
) -> tuple[bytes, str]:
    boundary = f"fsboundary{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode("utf-8")
            + b"\r\n"
        )
    safe_name = re.sub(r'[\r\n"\\/]+', "_", filename).strip(" .")[:150] or "file"
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe_name}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def graph_call(
    token: str,
    version: str,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    upload: dict[str, Any] | None = None,
    read_only: bool = False,
) -> dict[str, Any]:
    """One Graph API call: ``{"ok": True, "data": {...}}`` or a failure dict.

    ``upload`` is ``{"fields", "file_field", "filename", "content_type", "data"}`` and
    sends multipart/form-data. ``read_only`` calls never have an external effect, so a
    lost answer is a plain, retryable failure and not an ambiguous one.
    """
    if not token:
        return _failure(
            "access_token_required",
            "Save an access token on the Setup page first.",
            definite=True,
        )
    url = f"{GRAPH_BASE_URL}/{version}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{parse.urlencode(query)}"
    headers = {"Authorization": f"Bearer {token}", "Accept": APPLICATION_JSON_VALUE}
    body: bytes | None = None
    timeout: float = JSON_TIMEOUT_SECONDS
    if upload is not None:
        body, headers["Content-Type"] = _multipart_body(
            upload["fields"],
            file_field=upload["file_field"],
            filename=upload["filename"],
            content_type=upload["content_type"],
            data=upload["data"],
        )
        timeout = UPLOAD_TIMEOUT_SECONDS
    elif json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = APPLICATION_JSON_VALUE
    try:
        status, _headers, raw = _http_exchange(
            method, url, headers=headers, body=body, timeout=timeout, max_bytes=1024 * 1024
        )
    except Exception as exc:
        failure = ambiguous_failure(type(exc).__name__, token=token)
        if read_only:
            return {
                **failure,
                "error_code": "whatsapp_unreachable",
                "error": "WhatsApp could not be reached. Try again.",
                "definitely_no_external_effect": True,
                "external_effect_status": "failed",
                "retryable": True,
            }
        return failure
    parsed = _parse_json(raw)
    if status == 200 and "error" not in parsed:
        return {"ok": True, "data": parsed}
    return map_graph_error(status, parsed, token=token)


def send_graph_message(
    token: str, settings: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """POST one message body to ``/<phone_number_id>/messages``.

    Success is ``{"ok": True, "message_id": <wamid>}``; accepted means queued, not
    delivered (delivery arrives as a ``statuses`` webhook).
    """
    phone_number_id = _as_str(settings.get("phone_number_id"))
    if not phone_number_id:
        return _failure(
            "phone_number_id_required",
            "Save the phone number ID on the Setup page first.",
            definite=True,
        )
    body = {"messaging_product": "whatsapp", **payload}
    response = graph_call(
        token,
        _as_str(settings.get("graph_api_version")),
        "POST",
        f"{phone_number_id}/messages",
        json_body=body,
    )
    if not response.get("ok"):
        return response
    messages = response["data"].get("messages")
    first = _as_dict(messages[0]) if isinstance(messages, list) and messages else {}
    message_id = _as_str(first.get("id"))
    if not message_id:
        return ambiguous_failure("The answer held no message id", token=token)
    return {"ok": True, "message_id": message_id}


def upload_media(
    token: str,
    settings: dict[str, Any],
    *,
    filename: str,
    mime_type: str,
    data: bytes,
) -> dict[str, Any]:
    """POST ``/<phone_number_id>/media`` multipart; success is ``{"ok": True, "media_id"}``."""
    phone_number_id = _as_str(settings.get("phone_number_id"))
    if not phone_number_id:
        return _failure(
            "phone_number_id_required",
            "Save the phone number ID on the Setup page first.",
            definite=True,
        )
    response = graph_call(
        token,
        _as_str(settings.get("graph_api_version")),
        "POST",
        f"{phone_number_id}/media",
        upload={
            "fields": {"messaging_product": "whatsapp", "type": mime_type},
            "file_field": "file",
            "filename": filename,
            "content_type": mime_type,
            "data": data,
        },
    )
    if not response.get("ok"):
        return response
    media_id = _as_str(response["data"].get("id"))
    if not media_id:
        return ambiguous_failure("The answer held no media id", token=token)
    return {"ok": True, "media_id": media_id}


def get_media_info(token: str, settings: dict[str, Any], media_id: str) -> dict[str, Any]:
    """GET ``/<media_id>``: ``{"ok", "url", "mime_type", "file_size", "sha256"}``."""
    query = {}
    if _as_str(settings.get("phone_number_id")):
        query["phone_number_id"] = _as_str(settings.get("phone_number_id"))
    response = graph_call(
        token,
        _as_str(settings.get("graph_api_version")),
        "GET",
        parse.quote(media_id, safe=""),
        query=query,
        read_only=True,
    )
    if not response.get("ok"):
        return response
    data = response["data"]
    url = _as_str(data.get("url"))
    if not url:
        return _failure("whatsapp_media_not_found", "WhatsApp gave no download URL.", definite=True)
    return {
        "ok": True,
        "url": url,
        "mime_type": _as_str(data.get("mime_type")),
        "file_size": _coerce_int(data.get("file_size")),
        "sha256": _as_str(data.get("sha256")),
    }


def media_url_allowed(url: str) -> bool:
    """True for an https URL on a Meta media host; the bearer token goes nowhere else."""
    parsed = parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        return False
    return host == GRAPH_HOST or host.endswith(MEDIA_HOST_SUFFIXES)


def download_media(token: str, url: str, *, max_bytes: int) -> dict[str, Any]:
    """GET the media bytes from the temporary URL WITH the bearer token.

    Success is ``{"ok": True, "data": bytes, "content_type"}``. The URL lives 5 minutes;
    a 404 means "fetch a new URL" and is reported as ``whatsapp_media_not_found``.
    """
    if not media_url_allowed(url):
        return _failure(
            "whatsapp_media_url_rejected",
            "WhatsApp returned a download URL on an unexpected host; it was not requested.",
            definite=True,
        )
    try:
        status, headers, raw = _http_exchange(
            "GET",
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
            body=None,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
            max_bytes=max_bytes,
        )
    except ResponseTooLarge:
        return _failure(
            "file_too_large",
            f"The file is larger than {max_bytes // (1024 * 1024)} MB and is not cut.",
            definite=True,
        )
    except Exception as exc:
        return _failure(
            "whatsapp_download_failed",
            f"WhatsApp did not serve the file ({redact(type(exc).__name__, token)}).",
            definite=True,
            retryable=True,
        )
    if status == 404:
        return _failure(
            "whatsapp_media_not_found",
            "The media was not found: the download link lives only 5 minutes and media IDs "
            "from webhooks are kept 7 days. Run the step again soon after the message arrives.",
            definite=True,
            status=404,
        )
    if status != 200:
        return map_graph_error(status, _parse_json(raw), token=token)
    return {"ok": True, "data": raw, "content_type": _as_str(headers.get("content-type"))}


def get_phone_number(token: str, settings: dict[str, Any]) -> dict[str, Any]:
    """GET ``/<phone_number_id>`` for the Verify button."""
    phone_number_id = _as_str(settings.get("phone_number_id"))
    if not phone_number_id:
        return _failure(
            "phone_number_id_required",
            "Save the phone number ID on the Setup page first.",
            definite=True,
        )
    return graph_call(
        token,
        _as_str(settings.get("graph_api_version")),
        "GET",
        parse.quote(phone_number_id, safe=""),
        query={"fields": "verified_name,display_phone_number,quality_rating"},
        read_only=True,
    )


def _retryable_delivery_status(status: Any) -> bool:
    return _as_str(status).lower() in {"failed", "skipped"}


def _retry_error_message(error_code: Any) -> str:
    return {
        "whatsapp_window_closed": "The 24-hour window is closed; send a template instead.",
        "access_token_required": "Save an access token before retrying.",  # nosec B105 - an error message, not a secret
    }.get(_as_str(error_code), "Retry failed. Review the error and try again.")
