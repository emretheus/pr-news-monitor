"""Bounded public HTTP retrieval shared by discovery and extraction."""

import ipaddress
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from urllib.parse import unquote_plus, urljoin, urlsplit, urlunsplit

import certifi
import urllib3


class FetchError(ValueError):
    """Safe to display: never includes a request URL, credentials, or response body."""


@dataclass(frozen=True)
class Page:
    url: str
    text: str
    content_type: str


def normalize_url(url: str) -> str:
    """Remove known tracking only; preserve meaningful query values and their order."""
    if not isinstance(url, str) or any(ord(char) < 33 for char in url.strip()):
        raise FetchError("Invalid article URL.")
    try:
        parts = urlsplit(url.strip())
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
        ):
            raise ValueError
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        if port is not None and port not in (80, 443):
            raise ValueError
        netloc = f"[{host}]" if ":" in host else host
        if port and port != (443 if parts.scheme == "https" else 80):
            netloc += f":{port}"
        query = []
        for item in parts.query.split("&"):
            key = unquote_plus(item.partition("=")[0]).casefold()
            if (
                item
                and not key.startswith("utm_")
                and key not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
            ):
                query.append(item)
        return urlunsplit(
            (parts.scheme, netloc, parts.path or "/", "&".join(query), "")
        )
    except (ValueError, UnicodeError):
        raise FetchError(
            "URL must use HTTP(S), a valid hostname, and a standard web port."
        ) from None


def _public_address(host: str, port: int) -> str:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = list(dict.fromkeys(row[4][0] for row in addresses))
        if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
            raise FetchError(
                "Private or non-public network destinations are not allowed."
            )
        # Prefer IPv4 where available; do not resolve the hostname again at connect time.
        return next((ip for ip in ips if ":" not in ip), ips[0])
    except (OSError, ValueError) as exc:
        if isinstance(exc, FetchError):
            raise
        raise FetchError("Could not resolve a public destination.") from None


def _retry_delay(value: str | None) -> float:
    if value is None:
        return 0.5
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(
                0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            return 0.5


def fetch_page(
    url: str,
    *,
    deadline: float,
    max_bytes: int = 2_000_000,
    allow_redirects: bool = True,
) -> Page:
    """Fetch once plus at most one transient retry, with at most three redirects.

    `deadline` is an absolute time.monotonic() value shared by the caller's work.
    DNS uses the system resolver; its own timeout cannot be controlled here.
    """
    current = normalize_url(url)
    retries = 0
    redirects = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("Retrieval time budget exhausted.")
        parts = urlsplit(current)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        address = _public_address(parts.hostname, port)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError("Retrieval time budget exhausted.")
        timeout = urllib3.Timeout(connect=min(4, remaining), read=min(5, remaining))
        if parts.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(
                address,
                port=port,
                timeout=timeout,
                server_hostname=parts.hostname,
                assert_hostname=parts.hostname,
                cert_reqs="CERT_REQUIRED",
                ca_certs=certifi.where(),
            )
        else:
            pool = urllib3.HTTPConnectionPool(address, port=port, timeout=timeout)
        response = None
        retry_delay = None
        try:
            target = urlunsplit(("", "", parts.path, parts.query, ""))
            response = pool.urlopen(
                "GET",
                target,
                headers={
                    "Host": parts.netloc,
                    "User-Agent": "PRNewsMonitor/0.1",
                    "Accept-Encoding": "identity",
                },
                preload_content=False,
                redirect=False,
                retries=False,
                assert_same_host=False,
            )
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not allow_redirects or redirects >= 3 or not location:
                    raise FetchError("Redirect was blocked or redirect limit reached.")
                redirected = normalize_url(urljoin(current, location))
                if parts.scheme == "https" and urlsplit(redirected).scheme != "https":
                    raise FetchError("Insecure HTTPS downgrade redirect was blocked.")
                current = redirected
                redirects += 1
                continue
            if response.status == 429 or 500 <= response.status < 600:
                retry_delay = _retry_delay(response.headers.get("Retry-After"))
                if (
                    retries
                    or retry_delay > 2
                    or time.monotonic() + retry_delay >= deadline
                ):
                    raise FetchError(
                        f"Source temporarily unavailable (HTTP {response.status})."
                    )
            elif response.status != 200:
                raise FetchError(f"Source returned HTTP {response.status}.")
            else:
                chunks = bytearray()
                while True:
                    if time.monotonic() >= deadline:
                        raise FetchError("Retrieval time budget exhausted.")
                    chunk = response.read(
                        min(65_536, max_bytes + 1 - len(chunks)), decode_content=True
                    )
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        raise FetchError("Response exceeds the allowed size.")
                header = Message()
                header["content-type"] = response.headers.get(
                    "Content-Type", "text/html"
                )
                charset = header.get_content_charset() or "utf-8"
                try:
                    text = chunks.decode(charset, errors="replace")
                except LookupError:
                    text = chunks.decode("utf-8", errors="replace")
                return Page(current, text, header.get_content_type())
        except urllib3.exceptions.SSLError:
            raise FetchError(
                "The destination's TLS certificate could not be verified."
            ) from None
        except (urllib3.exceptions.HTTPError, OSError):
            if retries:
                raise FetchError("Network request failed or timed out.") from None
            retry_delay = 0.5
        finally:
            if response is not None:
                response.close()
            pool.close()
        if retry_delay is not None:
            if time.monotonic() + retry_delay >= deadline:
                raise FetchError("Retrieval time budget exhausted.")
            time.sleep(retry_delay)
            retries += 1
