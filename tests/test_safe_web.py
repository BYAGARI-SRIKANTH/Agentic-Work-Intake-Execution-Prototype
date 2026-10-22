from types import SimpleNamespace

from integrations import safe_web


def test_blocks_private_ip_before_connecting(monkeypatch):
    called = []
    monkeypatch.setattr(
        safe_web.socket,
        "create_connection",
        lambda *args, **kwargs: called.append((args, kwargs)),
    )

    result = safe_web.check_public_website("http://127.0.0.1/")

    assert result["reachable"] is False
    assert "blocked" in result["error"]
    assert called == []


def test_blocks_cloud_metadata_and_local_hostnames():
    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://168.63.129.16/",
        "http://metadata.google.internal/",
        "http://service.local/",
    ):
        result = safe_web.check_public_website(url)
        assert result["reachable"] is False


def test_pins_request_to_public_dns_result(monkeypatch):
    destinations = []

    class FakeConnection:
        def __init__(self, host, port, address):
            destinations.append((host, port, address))

        def request(self, *args, **kwargs):
            assert args[0] == "GET"
            assert kwargs["headers"]["User-Agent"]

        def getresponse(self):
            return SimpleNamespace(
                status=200,
                getheader=lambda name: None if name == "Location" else None,
                read=lambda size: b"" if size == 1024 else b"",
            )

        def close(self):
            pass

    monkeypatch.setattr(
        safe_web.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: (
            [(None, None, None, None, ("93.184.216.34", 443))]
            if (
                host == "example.com"
                and port == 443
                and kwargs.get("type") == safe_web.socket.SOCK_STREAM
            )
            else []
        ),
    )
    monkeypatch.setattr(safe_web, "_PinnedHTTPSConnection", FakeConnection)

    result = safe_web.check_public_website("https://example.com/health")

    assert result["reachable"] is True
    assert destinations == [("example.com", 443, "93.184.216.34")]


def test_redirect_to_private_destination_is_blocked(monkeypatch):
    class FakeConnection:
        def __init__(self, *_args):
            assert _args[0] == "example.com"

        def request(self, *args, **kwargs):
            assert args[0] == "GET"
            assert kwargs["headers"]["User-Agent"]

        def getresponse(self):
            return SimpleNamespace(
                status=302,
                getheader=lambda name: (
                    "http://127.0.0.1/admin" if name == "Location" else None
                ),
                read=lambda size: b"" if size == 1024 else b"",
            )

        def close(self):
            pass

    monkeypatch.setattr(
        safe_web.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: (
            [(None, None, None, None, ("93.184.216.34", 443))]
            if (
                host == "example.com"
                and port == 443
                and kwargs.get("type") == safe_web.socket.SOCK_STREAM
            )
            else []
        ),
    )
    monkeypatch.setattr(safe_web, "_PinnedHTTPSConnection", FakeConnection)

    result = safe_web.check_public_website("https://example.com/")

    assert result["reachable"] is False
    assert "blocked" in result["error"]


def test_rejects_custom_ports_and_query_tokens():
    assert not safe_web.check_public_website("https://example.com:8443")["reachable"]
    assert not safe_web.check_public_website("https://example.com/?token=secret")["reachable"]
