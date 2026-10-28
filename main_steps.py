"""Workflow steps that move files: ``send_file`` (upload, then send) and ``get_file``.

``send_file`` checks the per-kind size limit and the MIME type BEFORE it uploads anything,
uploads the artifact to ``/<phone_number_id>/media`` and sends the returned media ID. A file
that is too large is refused, never cut. ``get_file`` resolves a media ID to a temporary URL
(valid 5 minutes), downloads it with the bearer token at once and stores a Flow Steward
artifact.
"""

from __future__ import annotations

import mimetypes
from typing import Any

from flowsteward_extension_sdk import (
    ArtifactAccessError,
    find_artifact_descriptor,
    stream_artifact_bytes,
    write_artifact_stream,
)

from main_send import (
    _connection,
    _context,
    _failure,
    _recipient,
    _test_mode,
    _suppressed,
    resolve_contact,
    step_result,
)
from main_state import _as_dict, _as_str
from main_transport import download_media, get_media_info, send_graph_message, upload_media

UPLOAD_ARTIFACT_BINDING_KEY = "source_artifact"
DOWNLOAD_ARTIFACT_BINDING_KEY = "downloaded_file"
OCTET_STREAM = "application/octet-stream"
UPLOAD_CHUNK_BYTES = 1024 * 1024
CAPTION_LIMIT = 1024
MB = 1024 * 1024

# Facts file (Meta media reference): size limit and accepted MIME types per kind.
KIND_LIMITS_BYTES: dict[str, int] = {
    "image": 5 * MB,
    "document": 100 * MB,
    "audio": 16 * MB,
    "video": 16 * MB,
}
KIND_MIME_TYPES: dict[str, tuple[str, ...]] = {
    "image": ("image/jpeg", "image/png"),
    "document": (
        "text/plain",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/pdf",
    ),
    # audio/ogg is accepted only with the OPUS codec, mono; the extension cannot inspect the
    # file, so it passes it on and WhatsApp answers 131053 when it is not.
    "audio": ("audio/aac", "audio/amr", "audio/mpeg", "audio/mp4", "audio/ogg"),
    "video": ("video/3gpp", "video/mp4"),
}
DOWNLOAD_LIMIT_BYTES = 100 * MB  # the largest kind (document)

_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


def _artifact_handle(input_payload: dict[str, Any]) -> str:
    value = input_payload.get("artifact_handle") or input_payload.get("artifact_id")
    if isinstance(value, dict):
        value = value.get("artifact_handle") or value.get("artifact_id")
    return _as_str(value)


def _read_artifact(
    action: dict[str, Any], handle: str, limit: int
) -> tuple[bytes | None, dict[str, Any], tuple[dict[str, Any], int] | None]:
    """``(bytes, descriptor, None)`` of the input artifact, never more than ``limit`` bytes."""
    too_large = _failure(
        "file_too_large",
        f"WhatsApp accepts at most {limit // MB} MB for this kind of file; "
        "the file is larger and is not cut.",
    )
    try:
        descriptor = find_artifact_descriptor(
            action, artifact_id=handle, binding_key=UPLOAD_ARTIFACT_BINDING_KEY, role="input"
        )
        size = descriptor.get("size_bytes")
        if isinstance(size, int) and not isinstance(size, bool) and size > limit:
            return None, {}, too_large
        data = bytearray()
        for chunk in stream_artifact_bytes(
            action,
            artifact_id=handle,
            binding_key=UPLOAD_ARTIFACT_BINDING_KEY,
            chunk_size=UPLOAD_CHUNK_BYTES,
            timeout_seconds=60,
        ):
            data += chunk
            if len(data) > limit:
                return None, {}, too_large
    except ArtifactAccessError as exc:
        return None, {}, _failure("artifact_read_failed", f"The file could not be read: {exc}")
    return bytes(data), dict(descriptor), None


def _mime_type(input_payload: dict[str, Any], descriptor: dict[str, Any], filename: str) -> str:
    explicit = _as_str(input_payload.get("mime_type")).lower()
    if explicit:
        return explicit
    declared = _as_str(descriptor.get("content_type") or descriptor.get("mime_type")).lower()
    declared = declared.split(";")[0].strip()
    if declared and declared != OCTET_STREAM:
        return declared
    return _as_str(mimetypes.guess_type(filename)[0]).lower()


def handle_send_file(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run the ``send_file`` workflow operation."""
    input_payload = _as_dict(action.get("input"))
    kind = _as_str(input_payload.get("kind")).lower()
    if kind not in KIND_LIMITS_BYTES:
        return _failure(
            "kind_invalid",
            f"Kind must be one of: {', '.join(KIND_LIMITS_BYTES)}; got '{kind or 'nothing'}'.",
        )
    handle = _artifact_handle(input_payload)
    if not handle:
        return _failure("artifact_required", "Map the file (an artifact) before running this step.")
    caption = _as_str(input_payload.get("caption"))
    if caption and kind == "audio":
        return _failure("caption_not_supported", "WhatsApp does not show a caption on audio.")
    if len(caption) > CAPTION_LIMIT:
        return _failure(
            "caption_too_long",
            f"WhatsApp accepts at most {CAPTION_LIMIT} characters in a caption; "
            f"this one has {len(caption)}.",
        )
    if _test_mode(action, runtime_context):
        return _suppressed(_as_str(input_payload.get("channel_id")))
    resolved, failure = resolve_contact(action, input_payload)
    if failure is not None or resolved is None:
        return failure or _failure("channel_id_required", "Choose a contact by its ID.")
    token, contact, settings = resolved

    data, descriptor, failure = _read_artifact(action, handle, KIND_LIMITS_BYTES[kind])
    if failure is not None or data is None:
        return failure or _failure("artifact_read_failed", "The file could not be read.")
    if not data:
        return _failure("artifact_empty", "The file is empty; WhatsApp does not accept it.")
    artifact_name = _as_str(descriptor.get("filename") or descriptor.get("artifact_filename"))
    filename = _as_str(input_payload.get("file_name")) or artifact_name or f"{kind}.bin"
    mime_type = _mime_type(input_payload, descriptor, filename)
    if mime_type not in KIND_MIME_TYPES[kind]:
        return _failure(
            "mime_type_unsupported",
            f"WhatsApp does not accept '{mime_type or 'an unknown type'}' as {kind}. "
            f"Accepted: {', '.join(KIND_MIME_TYPES[kind])}.",
        )

    uploaded = upload_media(token, settings, filename=filename, mime_type=mime_type, data=data)
    if not uploaded.get("ok"):
        return step_result(uploaded, contact)
    media: dict[str, Any] = {"id": uploaded["media_id"]}
    if caption:
        media["caption"] = caption
    if kind == "document":
        media["filename"] = filename
    payload = {**_recipient(contact), **_context(input_payload), "type": kind, kind: media}
    return step_result(send_graph_message(token, settings, payload), contact)


def _safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "._- ").strip(" .")
    return cleaned[:150]


def handle_get_file(
    action: dict[str, Any], runtime_context: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Run ``get_file``: download a received media (by its media ID) into an artifact.

    The download link lives 5 minutes and is never stored; the step downloads at once.
    Media IDs received in webhooks are kept 7 days.
    """
    _ = runtime_context
    input_payload = _as_dict(action.get("input"))
    media_id = _as_str(input_payload.get("file_id"))
    if not media_id:
        return _failure("file_id_required", "Map the media ID (file_id) before running this step.")
    connection, token, settings = _connection(action)
    if not connection:
        return _failure(
            "project_connection_required", "Choose the WhatsApp connection this step uses."
        )
    if not token:
        return _failure("access_token_required", "The connection has no access token.")

    info = get_media_info(token, settings, media_id)
    if not info.get("ok"):
        return _read_failure(info)
    declared = info.get("file_size")
    if isinstance(declared, int) and declared > DOWNLOAD_LIMIT_BYTES:
        return _too_large()
    downloaded = download_media(token, _as_str(info.get("url")), max_bytes=DOWNLOAD_LIMIT_BYTES)
    if not downloaded.get("ok"):
        return _read_failure(downloaded)
    data: bytes = downloaded["data"]
    mime_type = _as_str(info.get("mime_type") or downloaded.get("content_type")).split(";")[0]
    mime_type = mime_type.strip().lower() or OCTET_STREAM
    name = _safe_name(_as_str(input_payload.get("file_name"))) or (
        f"whatsapp_{_safe_name(media_id)}{_EXTENSIONS.get(mime_type, '')}"
    )
    try:
        write_result = write_artifact_stream(
            action,
            iter([data]),
            binding_key=DOWNLOAD_ARTIFACT_BINDING_KEY,
            content_type=mime_type,
            timeout_seconds=60,
        )
    except ArtifactAccessError as exc:
        return _failure("artifact_write_failed", f"The file could not be stored: {exc}")
    handle = _as_str(write_result.get("artifact_handle") or write_result.get("artifact_id"))
    if not handle:
        return _failure("artifact_write_failed", "The artifact store returned no handle.")
    return (
        {
            "ok": True,
            "delivered": True,
            "artifact_handle": handle,
            "artifact_filename": name,
            "mime_type": mime_type,
            "size_bytes": write_result.get("size_bytes"),
            "sha256": _as_str(write_result.get("sha256")),
            "external_effect_status": "succeeded",
        },
        0,
    )


def _too_large() -> tuple[dict[str, Any], int]:
    return _failure(
        "file_too_large",
        f"The file is larger than {DOWNLOAD_LIMIT_BYTES // MB} MB, the largest WhatsApp media; "
        "it is not cut.",
    )


def _read_failure(response: dict[str, Any]) -> tuple[dict[str, Any], int]:
    result, code = _failure(
        _as_str(response.get("error_code")) or "whatsapp_request_failed",
        _as_str(response.get("error")) or "WhatsApp refused the request.",
    )
    for field in ("external_effect_status", "definitely_no_external_effect", "retryable"):
        if field in response:
            result[field] = response[field]
    return result, code


__all__ = [
    "KIND_LIMITS_BYTES",
    "KIND_MIME_TYPES",
    "handle_get_file",
    "handle_send_file",
]
