"""Shared fixtures: the bundle on sys.path and a fake Graph API transport.

Every test runs with the network function replaced: a test that does not use the ``graph``
fixture fails loudly if anything tries to reach the network. No real token exists here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

BUNDLE_ROOT = Path(__file__).resolve().parents[1]
for entry in (str(BUNDLE_ROOT), str(Path(__file__).resolve().parent)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import main_transport  # noqa: E402

TOKEN = "EAAtest-token-0123456789"  # pragma: allowlist secret
APP_SECRET = "test-app-secret-value"  # pragma: allowlist secret
PHONE_NUMBER_ID = "106540352242922"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"


class FakeGraph:
    """Scripted answers for :func:`main_transport._http_exchange`; records every request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._queue: list[Any] = []

    def reply(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self._queue.append((status, headers or {}, raw))

    def fail(self, exc: Exception) -> None:
        self._queue.append(exc)

    def __call__(self, method, url, *, headers, body, timeout, max_bytes):
        self.requests.append(
            {"method": method, "url": url, "headers": headers, "body": body, "max_bytes": max_bytes}
        )
        if not self._queue:
            raise AssertionError(f"unexpected request {method} {url}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, response_headers, raw = item
        if len(raw) > max_bytes:
            raise main_transport.ResponseTooLarge("too large")
        return status, response_headers, raw

    def json_body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index]["body"])


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*_a: Any, **_k: Any):
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(main_transport, "_http_exchange", _blocked)


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
    fake = FakeGraph()
    monkeypatch.setattr(main_transport, "_http_exchange", fake)
    return fake


def ok_message(wamid: str = "wamid.OUT1") -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "37060000001", "wa_id": "37060000001"}],
        "messages": [{"id": wamid}],
    }


def graph_error(code: int, message: str = "boom", **extra: Any) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "OAuthException",
            "code": code,
            "fbtrace_id": "T",
            **extra,
        }
    }
