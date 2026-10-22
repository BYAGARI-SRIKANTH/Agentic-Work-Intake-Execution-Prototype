import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit


_TIMEOUT_SECONDS = 8
_MAX_REDIRECTS = 3
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_BLOCKED_SERVICE_IPS = {"168.63.129.16"}


class UnsafeDestinationError(ValueError):
    pass


def _display_url(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "invalid URL"
    if not parsed.hostname:
        return "invalid URL"
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return f"{parsed.scheme}://{host}"
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = f":{port}" if port and port != default_port else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _resolve_public_address(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeDestinationError("Only HTTP and HTTPS destinations are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeDestinationError("URLs containing credentials are not allowed.")
    if parsed.query or parsed.fragment:
        raise UnsafeDestinationError("URLs containing query strings or fragments are not allowed.")

    host = parsed.hostname
    normalized_host = host.rstrip(".").lower() if host else ""
    if (
        not host
        or normalized_host in {"localhost", "metadata.google.internal"}
        or normalized_host.endswith((".localhost", ".local", ".internal"))
    ):
        raise UnsafeDestinationError("Local and metadata destinations are not allowed.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UnsafeDestinationError("The website hostname is invalid.") from exc
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise UnsafeDestinationError("Only standard HTTP and HTTPS ports are allowed.")

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        try:
            records = socket.getaddrinfo(
                host,
                port,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise UnsafeDestinationError("The website hostname could not be resolved.") from exc
        addresses = {record[4][0] for record in records}
    else:
        addresses = {str(literal_address)}

    if not addresses:
        raise UnsafeDestinationError("The website hostname has no usable address.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global or ip.compressed in _BLOCKED_SERVICE_IPS:
            raise UnsafeDestinationError("Private or reserved network destinations are blocked.")

    return parsed, host, port, sorted(addresses)[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address):
        super().__init__(host, port, timeout=_TIMEOUT_SECONDS)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection(
            (self._address, self.port),
            timeout=self.timeout,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, address):
        super().__init__(
            host,
            port,
            timeout=_TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
        self._address = address

    def connect(self):
        raw_socket = socket.create_connection(
            (self._address, self.port),
            timeout=self.timeout,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except (OSError, ssl.SSLError, ValueError):
            raw_socket.close()
            raise


def check_public_website(url):
    started = time.perf_counter()
    current_url = url

    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            parsed, host, port, address = _resolve_public_address(current_url)
            connection_type = (
                _PinnedHTTPSConnection
                if parsed.scheme == "https"
                else _PinnedHTTPConnection
            )
            connection = connection_type(host, port, address)
            path = urlunsplit(("", "", parsed.path or "/", "", ""))
            try:
                connection.request(
                    "GET",
                    path,
                    headers={
                        "User-Agent": "Altibbe-Agent-Prototype/1.0",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                status_code = response.status
                location = response.getheader("Location")
                response.read(1024)
            finally:
                connection.close()

            if status_code not in _REDIRECT_CODES or not location:
                elapsed = round(time.perf_counter() - started, 3)
                return {
                    "url": _display_url(url),
                    "final_url": _display_url(current_url),
                    "status_code": status_code,
                    "reachable": status_code < 500,
                    "response_time_seconds": elapsed,
                }
            if redirect_count == _MAX_REDIRECTS:
                return {
                    "url": _display_url(url),
                    "final_url": _display_url(current_url),
                    "reachable": False,
                    "error": "The website exceeded the redirect limit.",
                }
            current_url = urljoin(current_url, location)

    except UnsafeDestinationError as exc:
        return {
            "url": _display_url(url),
            "reachable": False,
            "error": str(exc),
        }
    except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
        return {
            "url": _display_url(url),
            "reachable": False,
            "error": "The website could not be checked safely.",
        }

    return {
        "url": _display_url(url),
        "reachable": False,
        "error": "The website could not be checked safely.",
    }
