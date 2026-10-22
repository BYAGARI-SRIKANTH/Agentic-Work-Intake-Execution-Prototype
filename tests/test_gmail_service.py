import base64
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import pytest
import google_auth_httplib2
import httplib2
from googleapiclient.errors import HttpError

from integrations.errors import IntegrationError
from integrations import gmail_service
from integrations.gmail_service import (
    GMAIL_HTTP_TIMEOUT,
    compose_email_message,
    send_email,
)


def test_compose_email_message_contains_plain_text_fields():
    raw_message = compose_email_message(
        "person@example.com",
        "Project update",
        "The task is ready.",
    )
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw_message)
    )

    assert message["To"] == "person@example.com"
    assert message["Subject"] == "Project update"
    assert message.get_content_type() == "text/plain"
    assert message.get_content().strip() == "The task is ready."


def test_compose_email_message_supports_multiple_normalized_recipients():
    raw_message = compose_email_message(
        " first@example.com , second@EXAMPLE.COM ",
        "Project update",
        "The task is ready.",
    )
    message = BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw_message)
    )

    assert message["To"] == "first@example.com, second@example.com"


@pytest.mark.parametrize(
    "recipient",
    [
        "",
        "  ",
        "first@example.com,,second@example.com",
        "first@example.com, not-an-address",
        "first@example.com, FIRST@example.com",
        "Person <first@example.com>",
    ],
)
def test_invalid_recipient_lists_are_rejected(recipient):
    with pytest.raises(IntegrationError, match="valid, unique email recipients"):
        compose_email_message(recipient, "Subject", "Body")


def test_missing_recipient_is_rejected_before_authentication():
    with pytest.raises(IntegrationError, match="recipient"):
        send_email("", "Subject", "Body", token_file="missing-token.json")


def test_missing_gmail_authorization_fails_safely(tmp_path):
    with pytest.raises(IntegrationError, match="not authorized") as error:
        send_email(
            "person@example.com",
            "Subject",
            "Body",
            token_file=tmp_path / "missing-token.json",
        )

    assert error.value.category == "authentication"


def test_gmail_api_failure_is_not_reported_as_sent():
    class Response:
        status = 500
        reason = "Internal server error"

    class SendRequest:
        def execute(self):
            raise HttpError(Response(), b"private API response")

    class Messages:
        def send(self, **kwargs):
            assert kwargs["userId"] == "me"
            assert kwargs["body"]["raw"]
            return SendRequest()

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    with pytest.raises(IntegrationError) as error:
        send_email(
            "person@example.com",
            "Subject",
            "Body",
            service=Service(),
        )

    assert error.value.category == "api"
    assert error.value.outcome_unknown is True
    assert "private API response" not in str(error.value)


def test_gmail_authentication_failure_is_reported_safely():
    class Response:
        status = 403
        reason = "Forbidden"

    class SendRequest:
        def execute(self):
            raise HttpError(Response(), b"private authorization details")

    class Messages:
        def send(self, **kwargs):
            assert kwargs["userId"] == "me"
            assert kwargs["body"]["raw"]
            return SendRequest()

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    with pytest.raises(IntegrationError) as error:
        send_email(
            "person@example.com",
            "Subject",
            "Body",
            service=Service(),
        )

    assert error.value.category == "authentication"
    assert "private authorization details" not in str(error.value)


def test_gmail_success_requires_confirmed_message_id():
    class SendRequest:
        def execute(self):
            return {"id": "gmail-message-123"}

    class Messages:
        def send(self, **kwargs):
            assert kwargs["userId"] == "me"
            assert kwargs["body"]["raw"]
            return SendRequest()

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    result = send_email(
        "person@example.com",
        "Subject",
        "Body",
        service=Service(),
    )

    assert result == {"message_id": "gmail-message-123"}


def test_gmail_service_initialization_uses_authorized_http_and_timeout(monkeypatch):
    class Credentials:
        pass

    class Request:
        def __init__(self, uri, method, body):
            self.uri = uri
            self.method = method
            self.body = body

        def execute(self):
            return {"id": "gmail-message-456"}

    class Messages:
        def send(self, userId, body):
            assert userId == "me"
            return Request("https://gmail.googleapis.com/send", "POST", body)

    class Users:
        def messages(self):
            return Messages()

    class Service:
        def users(self):
            return Users()

    captured = {}
    credentials = Credentials()
    monkeypatch.setattr(gmail_service, "_load_credentials", lambda _path: credentials)

    authorized_http = object()
    http_transport = object()
    authorized_http_factory = Mock(return_value=authorized_http)
    http_factory = Mock(return_value=http_transport)
    monkeypatch.setattr(
        google_auth_httplib2,
        "AuthorizedHttp",
        authorized_http_factory,
    )
    monkeypatch.setattr(httplib2, "Http", http_factory)

    def build(api_name, version, *, http, credentials=None, cache_discovery):
        assert credentials is None
        captured.update(
            api_name=api_name,
            version=version,
            http=http,
            cache_discovery=cache_discovery,
        )
        return Service()

    monkeypatch.setattr(gmail_service, "build", build)

    result = send_email(
        "person@example.com",
        "Subject",
        "Body",
        token_file="test-token.json",
    )

    assert result == {"message_id": "gmail-message-456"}
    assert captured["api_name"] == "gmail"
    assert captured["version"] == "v1"
    assert captured["cache_discovery"] is False
    assert captured["http"] is authorized_http
    http_factory.assert_called_once_with(timeout=GMAIL_HTTP_TIMEOUT)
    authorized_http_factory.assert_called_once_with(
        credentials,
        http=http_transport,
    )
