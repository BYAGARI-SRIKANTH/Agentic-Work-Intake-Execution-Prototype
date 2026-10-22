import os
from datetime import date, datetime, timezone
from urllib.parse import urlparse

import requests

from integrations.errors import IntegrationError


TRELLO_CARDS_URL = "https://api.trello.com/1/cards"
REQUEST_TIMEOUT = (3.05, 10)


def normalize_due_date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise IntegrationError("validation", "Due date must be an ISO date or timestamp.")
    try:
        try:
            parsed_date = date.fromisoformat(value)
            parsed_datetime = datetime.combine(
                parsed_date,
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
        except ValueError:
            parsed_datetime = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed_datetime.tzinfo is None:
                raise ValueError("A timestamp must include a timezone.")
    except ValueError as exc:
        raise IntegrationError(
            "validation",
            "Due date must be YYYY-MM-DD or an ISO timestamp with a timezone.",
        ) from exc

    return (
        parsed_datetime.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", ".000Z")
    )


def create_trello_card(
    title,
    description,
    due_date=None,
    *,
    api_key=None,
    token=None,
    list_id=None,
    session=None,
):
    if not isinstance(title, str) or not title.strip():
        raise IntegrationError("validation", "Trello task title is required.")
    if not isinstance(description, str) or not description.strip():
        raise IntegrationError("validation", "Trello task description is required.")
    normalized_due = normalize_due_date(due_date)

    api_key = api_key or os.getenv("TRELLO_API_KEY", "").strip()
    token = token or os.getenv("TRELLO_TOKEN", "").strip()
    list_id = list_id or os.getenv("TRELLO_LIST_ID", "").strip()
    if not api_key or not token or not list_id:
        raise IntegrationError(
            "authentication",
            "Trello is not configured. Set TRELLO_API_KEY, TRELLO_TOKEN, "
            "and TRELLO_LIST_ID.",
        )

    params = {
        "key": api_key,
        "token": token,
        "idList": list_id,
        "name": title.strip(),
        "desc": description,
    }
    if normalized_due:
        params["due"] = normalized_due

    client = session or requests.Session()
    try:
        response = client.post(
            TRELLO_CARDS_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise IntegrationError(
            "network",
            "Trello did not confirm card creation. Check Trello before retrying.",
            outcome_unknown=True,
        ) from exc

    if response.status_code in (401, 403):
        raise IntegrationError(
            "authentication",
            "Trello rejected the configured credentials or permissions.",
        )
    if response.status_code == 429:
        raise IntegrationError(
            "rate_limit",
            "Trello rate-limited the request. No automatic retry was attempted.",
        )
    if response.status_code >= 500:
        raise IntegrationError(
            "api",
            "Trello did not confirm card creation. Check Trello before retrying.",
            outcome_unknown=True,
        )
    if not response.ok:
        raise IntegrationError(
            "api",
            "Trello rejected the card request. Review the task fields and settings.",
        )

    try:
        result = response.json()
    except (ValueError, requests.exceptions.JSONDecodeError) as exc:
        raise IntegrationError(
            "api",
            "Trello returned an unreadable response; card creation is unconfirmed.",
            outcome_unknown=True,
        ) from exc

    card_id = result.get("id") if isinstance(result, dict) else None
    card_url = result.get("url") if isinstance(result, dict) else None
    parsed_url = urlparse(card_url) if isinstance(card_url, str) else None
    if (
        not isinstance(card_id, str)
        or not card_id.strip()
        or parsed_url is None
        or parsed_url.scheme != "https"
        or parsed_url.hostname not in {"trello.com", "www.trello.com"}
    ):
        raise IntegrationError(
            "api",
            "Trello returned no valid card ID and URL; the outcome is unknown.",
            outcome_unknown=True,
        )

    return {"card_id": card_id.strip(), "card_url": card_url}
