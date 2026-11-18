"""SSRF / DNS-rebinding regressions for remote document retrieval (MWB-008)."""

from __future__ import annotations

import socket

import pytest

from mayaworkbench.errors import FetchError
from mayaworkbench.research import fetch as fetch_module
from mayaworkbench.research.fetch import DocumentFetcher

PUBLIC = "93.184.216.34"
PRIVATE = "127.0.0.1"


def _resolver(*answers):
    calls: list[tuple[str, int]] = []

    def fake(host, port, *args, **kwargs):
        calls.append((host, port))
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, port))]

    return fake, calls


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b""):
        self.status = status
        self._headers = {key.lower(): value for key, value in headers.items()}
        self._body = body

    def getheader(self, name: str, default=None):
        return self._headers.get(name.lower(), default)

    def read(self, _amount: int | None = None) -> bytes:
        body, self._body = self._body, b""
        return body


class _Connection:
    def __init__(self, address: str, response: _Response | None, error: Exception | None = None):
        self.address = address
        self.response = response
        self.error = error
        self.closed = False

    def request(self, *_args, **_kwargs) -> None:
        if self.error is not None:
            raise self.error

    def getresponse(self) -> _Response:
        assert self.response is not None
        return self.response

    def close(self) -> None:
        self.closed = True


def test_fetch_connects_to_the_validated_address_not_a_second_resolution(
    monkeypatch,
):
    """A second, private DNS answer must never be used for the connection."""
    resolver, calls = _resolver(PRIVATE if False else PUBLIC, PRIVATE)
    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", resolver)
    seen: list[str] = []

    def factory(scheme, host, port, address, timeout):
        seen.append(address)
        return _Connection(address, None, OSError("connect blocked in test"))

    monkeypatch.setattr(fetch_module, "_pinned_connection", factory)
    fetcher = DocumentFetcher(timeout_seconds=2)
    with pytest.raises(FetchError):
        fetcher.fetch("http://rebind.example/paper.txt")
    assert seen == [PUBLIC]
    assert len(calls) == 1


def test_mixed_public_and_private_dns_answers_are_refused(monkeypatch):
    def fake(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PRIVATE, port)),
        ]

    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", fake)
    fetcher = DocumentFetcher(timeout_seconds=2)
    with pytest.raises(FetchError, match="non-public address"):
        fetcher.fetch("http://mixed.example/paper.txt")


def test_redirect_to_a_private_destination_is_refused(monkeypatch):
    resolver, calls = _resolver(PUBLIC, PRIVATE)
    monkeypatch.setattr(fetch_module.socket, "getaddrinfo", resolver)
    created: list[str] = []

    def factory(scheme, host, port, address, timeout):
        created.append(address)
        return _Connection(
            address,
            _Response(302, {"Location": "http://internal.example/secret.txt"}),
        )

    monkeypatch.setattr(fetch_module, "_pinned_connection", factory)
    fetcher = DocumentFetcher(timeout_seconds=2, max_redirects=2)
    with pytest.raises(FetchError, match="non-public address"):
        fetcher.fetch("http://public.example/paper.txt")
    # The public hop was attempted once; the private hop was never connected to.
    assert created == [PUBLIC]
    assert len(calls) == 2


def test_loopback_url_is_refused_before_any_connection(monkeypatch):
    created: list[str] = []
    monkeypatch.setattr(
        fetch_module,
        "_pinned_connection",
        lambda *args: created.append(args[3]),
    )
    fetcher = DocumentFetcher(timeout_seconds=2)
    with pytest.raises(FetchError, match="localhost"):
        fetcher.fetch("http://localhost/paper.txt")
    assert created == []
