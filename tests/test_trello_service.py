import pytest
import requests

from integrations.errors import IntegrationError
from integrations.trello_service import (
    REQUEST_TIMEOUT,
    TRELLO_CARDS_URL,
    create_trello_card,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse(
            payload={
                "id": "card-123",
                "url": "https://trello.com/c/short123/card-title",
            }
        )
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def configured_card(session, **kwargs):
    return create_trello_card(
        "Review proposal",
        "Review the incoming proposal.",
        api_key="api-key",
        token="user-token",
        list_id="list-123",
        session=session,
        **kwargs,
    )


def test_card_creation_uses_documented_request_fields_and_returns_confirmed_ids():
    session = FakeSession()

    result = configured_card(session, due_date="2026-10-20")

    url, request = session.calls[0]
    assert url == TRELLO_CARDS_URL
    assert request["timeout"] == REQUEST_TIMEOUT
    assert request["params"]["idList"] == "list-123"
    assert request["params"]["name"] == "Review proposal"
    assert request["params"]["desc"] == "Review the incoming proposal."
    assert request["params"]["due"] == "2026-10-20T00:00:00.000Z"
    assert result == {
        "card_id": "card-123",
        "card_url": "https://trello.com/c/short123/card-title",
    }


def test_missing_title_is_rejected_before_request():
    session = FakeSession()

    with pytest.raises(IntegrationError, match="title"):
        create_trello_card(
            "",
            "Description",
            api_key="api-key",
            token="user-token",
            list_id="list-123",
            session=session,
        )

    assert session.calls == []


def test_invalid_due_date_is_rejected_before_request():
    session = FakeSession()

    with pytest.raises(IntegrationError, match="Due date"):
        configured_card(session, due_date="tomorrow")

    assert session.calls == []


def test_missing_credentials_are_reported_without_request():
    session = FakeSession()

    with pytest.raises(IntegrationError) as error:
        create_trello_card("Title", "Description", session=session)

    assert error.value.category == "authentication"
    assert session.calls == []


def test_trello_authentication_failure_is_sanitized():
    session = FakeSession(response=FakeResponse(status_code=401))

    with pytest.raises(IntegrationError, match="rejected") as error:
        configured_card(session)

    assert error.value.category == "authentication"
    assert "user-token" not in str(error.value)


def test_trello_api_failure_is_not_reported_as_created():
    session = FakeSession(response=FakeResponse(status_code=400))

    with pytest.raises(IntegrationError) as error:
        configured_card(session)

    assert error.value.category == "api"
    assert error.value.outcome_unknown is False


def test_trello_timeout_has_uncertain_outcome_and_is_not_retried():
    session = FakeSession(error=requests.Timeout("private request URL"))

    with pytest.raises(IntegrationError) as error:
        configured_card(session)

    assert error.value.outcome_unknown is True
    assert len(session.calls) == 1
    assert "private request URL" not in str(error.value)


def test_unconfirmed_trello_server_error_is_not_success():
    session = FakeSession(response=FakeResponse(status_code=503))

    with pytest.raises(IntegrationError) as error:
        configured_card(session)

    assert error.value.outcome_unknown is True


def test_trello_rate_limit_is_not_retried():
    session = FakeSession(response=FakeResponse(status_code=429))

    with pytest.raises(IntegrationError) as error:
        configured_card(session)

    assert error.value.category == "rate_limit"
    assert error.value.outcome_unknown is False
    assert len(session.calls) == 1
