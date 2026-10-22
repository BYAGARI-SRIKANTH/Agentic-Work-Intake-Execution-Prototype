import base64
import os
import re
import tempfile
from email.message import EmailMessage
from pathlib import Path

import google.auth.exceptions
import google_auth_httplib2
import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from integrations.errors import IntegrationError


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_LOCAL_PART_PATTERN = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_DOMAIN_LABEL_PATTERN = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
GMAIL_HTTP_TIMEOUT = 10


def normalize_recipients(recipient):
    error_message = "Enter one or more valid, unique email recipients."
    if not isinstance(recipient, str):
        raise IntegrationError("validation", error_message)

    entries = recipient.split(",")
    if not recipient.strip() or any(not entry.strip() for entry in entries):
        raise IntegrationError("validation", error_message)

    normalized = []
    seen = set()
    for entry in entries:
        address = entry.strip()
        if address.count("@") != 1:
            raise IntegrationError("validation", error_message)

        local_part, domain = address.rsplit("@", 1)
        labels = domain.split(".")
        if (
            len(address) > 254
            or len(local_part) > 64
            or not _LOCAL_PART_PATTERN.fullmatch(local_part)
            or local_part.startswith(".")
            or local_part.endswith(".")
            or ".." in local_part
            or len(labels) < 2
            or any(
                len(label) > 63 or not _DOMAIN_LABEL_PATTERN.fullmatch(label)
                for label in labels
            )
        ):
            raise IntegrationError("validation", error_message)

        normalized_address = f"{local_part}@{domain.lower()}"
        duplicate_key = normalized_address.casefold()
        if duplicate_key in seen:
            raise IntegrationError("validation", error_message)
        seen.add(duplicate_key)
        normalized.append(normalized_address)

    return ", ".join(normalized)


def compose_email_message(recipient, subject, body):
    recipients = normalize_recipients(recipient)
    if not isinstance(subject, str) or not subject.strip():
        raise IntegrationError("validation", "Email subject is required.")
    if "\r" in subject or "\n" in subject:
        raise IntegrationError("validation", "Email subject contains invalid characters.")
    if not isinstance(body, str) or not body.strip():
        raise IntegrationError("validation", "Email body is required.")

    message = EmailMessage()
    message["To"] = recipients
    message["Subject"] = subject.strip()
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _persist_refreshed_token(credentials, token_file):
    token_path = Path(token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=token_path.parent,
            prefix=f".{token_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as token_stream:
            temporary_path = Path(token_stream.name)
            token_stream.write(credentials.to_json())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, token_path)
    except OSError as exc:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        raise IntegrationError(
            "authentication",
            "The refreshed Gmail authorization could not be saved securely.",
        ) from exc


def _load_credentials(token_file):
    token_path = Path(token_file)
    if not token_path.is_file():
        raise IntegrationError(
            "authentication",
            "Gmail is not authorized. For local development, configure OAuth "
            "credentials and run `python -m integrations.gmail_auth`.",
        )

    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_path),
            [GMAIL_SEND_SCOPE],
        )
    except (OSError, ValueError, google.auth.exceptions.GoogleAuthError) as exc:
        raise IntegrationError(
            "authentication",
            "The saved Gmail authorization is missing or invalid.",
        ) from exc

    if credentials.expired:
        if not credentials.refresh_token:
            raise IntegrationError(
                "authentication",
                "Gmail authorization has expired. Re-authorize the local account.",
            )
        try:
            credentials.refresh(Request())
        except (google.auth.exceptions.GoogleAuthError, OSError) as exc:
            raise IntegrationError(
                "authentication",
                "Gmail authorization could not be refreshed.",
            ) from exc
        _persist_refreshed_token(credentials, token_path)

    if not credentials.valid:
        raise IntegrationError(
            "authentication",
            "Gmail authorization is not valid. Re-authorize the local account.",
        )
    return credentials


def send_email(recipient, subject, body, *, token_file=None, service=None):
    raw_message = compose_email_message(recipient, subject, body)

    if service is None:
        configured_token = token_file or os.getenv("GMAIL_TOKEN_FILE", "token.json")
        credentials = _load_credentials(configured_token)
        try:
            service = build(
                "gmail",
                "v1",
                http=google_auth_httplib2.AuthorizedHttp(
                    credentials,
                    http=httplib2.Http(timeout=GMAIL_HTTP_TIMEOUT),
                ),
                cache_discovery=False,
            )
        except (google.auth.exceptions.GoogleAuthError, OSError) as exc:
            raise IntegrationError(
                "authentication",
                "The Gmail API client could not be initialized.",
            ) from exc

    try:
        response = (
            service.users()
            .messages()
            .send(userId="me", body={"raw": raw_message})
            .execute()
        )
    except HttpError as exc:
        status = getattr(exc.resp, "status", None)
        if status in (401, 403):
            raise IntegrationError(
                "authentication",
                "Gmail rejected the authorization or send permission.",
            ) from exc
        raise IntegrationError(
            "api",
            "Gmail did not confirm the email submission.",
            outcome_unknown=status is None or status >= 500,
        ) from exc
    except (httplib2.HttpLib2Error, OSError, TimeoutError) as exc:
        raise IntegrationError(
            "network",
            "Gmail could not confirm the email submission.",
            outcome_unknown=True,
        ) from exc

    message_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(message_id, str) or not message_id.strip():
        raise IntegrationError(
            "api",
            "Gmail returned no message ID, so the send outcome is unknown.",
            outcome_unknown=True,
        )
    return {"message_id": message_id.strip()}