import io
import socket
import time
from unittest.mock import Mock

import pytest

from monitor import http


def test_normalization_preserves_meaningful_and_signed_query_bytes():
    assert (
        http.normalize_url(
            "HTTPS://Example.com:443/story?id=2&utm_source=x&sig=a%2Bb#top"
        )
        == "https://example.com/story?id=2&sig=a%2Bb"
    )
    assert http.normalize_url("https://example.com/story?id=3") != http.normalize_url(
        "https://example.com/story?id=2"
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "file:///etc/passwd",
        "http://user:secret@example.com",
        "http://example.com:22",
        "http://example.com/\nheader",
    ],
)
def test_rejects_invalid_urls(url):
    with pytest.raises(http.FetchError):
        http.normalize_url(url)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fc00::1"]
)
def test_rejects_private_resolved_addresses(monkeypatch, address):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, (address, 443))],
    )
    with pytest.raises(http.FetchError, match="non-public"):
        http._public_address("example.com", 443)


def response(status=200, body=b"article", headers=None):
    stream = io.BytesIO(body)
    return Mock(
        status=status,
        headers=headers or {"Content-Type": "text/html"},
        read=lambda size, **kwargs: stream.read(size),
    )


def wire(monkeypatch, replies):
    pool = Mock()
    pool.urlopen.side_effect = replies
    constructor = Mock(return_value=pool)
    monkeypatch.setattr(http.urllib3, "HTTPSConnectionPool", constructor)
    monkeypatch.setattr(http, "_public_address", lambda host, port: "93.184.216.34")
    return pool, constructor


def test_connects_to_validated_ip_with_original_tls_hostname(monkeypatch):
    pool, constructor = wire(monkeypatch, [response()])
    page = http.fetch_page(
        "https://example.com/article", deadline=time.monotonic() + 10
    )
    assert page.text == "article"
    assert constructor.call_args.args == ("93.184.216.34",)
    assert constructor.call_args.kwargs["server_hostname"] == "example.com"
    assert constructor.call_args.kwargs["assert_hostname"] == "example.com"
    assert pool.urlopen.call_args.kwargs["headers"]["Host"] == "example.com"


def test_redirect_is_revalidated_before_connection(monkeypatch):
    pool, _ = wire(
        monkeypatch, [response(302, headers={"Location": "https://127.0.0.1/private"})]
    )
    resolver = Mock(
        side_effect=["93.184.216.34", http.FetchError("Private destination")]
    )
    monkeypatch.setattr(http, "_public_address", resolver)
    with pytest.raises(http.FetchError, match="Private"):
        http.fetch_page("https://example.com/", deadline=time.monotonic() + 10)
    assert pool.urlopen.call_count == 1


def test_credential_bearing_request_does_not_follow_redirect(monkeypatch):
    pool, _ = wire(
        monkeypatch, [response(302, headers={"Location": "https://other.example/"})]
    )
    with pytest.raises(http.FetchError) as error:
        http.fetch_page(
            "https://example.com/?apikey=TOP_SECRET",
            deadline=time.monotonic() + 10,
            allow_redirects=False,
        )
    assert "TOP_SECRET" not in str(error.value)
    assert pool.urlopen.call_count == 1


def test_response_size_is_bounded_and_connection_closed(monkeypatch):
    reply = response(body=b"x" * 20)
    pool, _ = wire(monkeypatch, [reply])
    with pytest.raises(http.FetchError, match="size"):
        http.fetch_page(
            "https://example.com/", deadline=time.monotonic() + 10, max_bytes=10
        )
    reply.close.assert_called_once()
    pool.close.assert_called_once()


def test_transient_failure_retries_only_once(monkeypatch):
    pool, _ = wire(monkeypatch, [response(503), response(503)])
    monkeypatch.setattr(http.time, "sleep", lambda _: None)
    with pytest.raises(http.FetchError, match="503"):
        http.fetch_page("https://example.com/", deadline=time.monotonic() + 10)
    assert pool.urlopen.call_count == 2


@pytest.mark.parametrize(
    "status,headers", [(401, {}), (403, {}), (429, {"Retry-After": "60"})]
)
def test_permanent_errors_or_long_backoff_do_not_retry(monkeypatch, status, headers):
    pool, _ = wire(monkeypatch, [response(status, headers=headers)])
    with pytest.raises(http.FetchError):
        http.fetch_page("https://example.com/", deadline=time.monotonic() + 10)
    assert pool.urlopen.call_count == 1


def test_expired_deadline_makes_no_connection(monkeypatch):
    pool, _ = wire(monkeypatch, [])
    with pytest.raises(http.FetchError, match="budget"):
        http.fetch_page("https://example.com/", deadline=time.monotonic() - 1)
    pool.urlopen.assert_not_called()
