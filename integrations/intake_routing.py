import re

from integrations.errors import IntegrationError
from integrations.gmail_service import compose_email_message, normalize_recipients
from integrations.trello_service import normalize_due_date


VALID_CATEGORIES = {
    "execute_automatically",
    "prepare_for_human_review",
    "cannot_execute_with_tools",
    "requires_clarification",
}

VALID_TOOLS = {
    "check_website_status",
    "create_task_record",
    "draft_communication",
    "send_email",
    "create_trello_card",
    "none",
}

VALID_PARAMETER_KEYS = {
    "url",
    "title",
    "deadline",
    "priority",
    "recipient",
    "subject",
    "body",
    "description",
    "due_date",
}

_DEFERRED_SEND_PATTERN = re.compile(
    r"\b(?:do\s+not|don't|never)?\s*(?:send|sending|deliver|dispatch)\b"
    r".{0,120}\b(?:until|after|once|when)\b.{0,80}"
    r"\b(?:approve|approval|authorize|authorization|confirm)\w*\b",
    re.IGNORECASE | re.DOTALL,
)
_NEGATED_SEND_PREFIX = re.compile(
    r"\b(?:do\s+not|don't|never|not|cannot|can't|"
    r"should(?:\s+not|n't)|avoid)\b(?:\s+\w+){0,4}\s*$",
    re.IGNORECASE,
)

_ACTION_FIELDS = {
    "url": {"check_website_status"},
    "website": {"check_website_status"},
    "website url": {"check_website_status"},
    "title": {"create_task_record", "create_trello_card"},
    "task title": {"create_task_record", "create_trello_card"},
    "deadline": set(),
    "priority": set(),
    "recipient": {"draft_communication", "send_email"},
    "email recipient": {"draft_communication", "send_email"},
    "subject": {"draft_communication", "send_email"},
    "email subject": {"draft_communication", "send_email"},
    "body": {"draft_communication", "send_email"},
    "email body": {"draft_communication", "send_email"},
    "description": {"create_trello_card"},
    "due date": set(),
    "due_date": set(),
}

_FIELD_KEYS = {
    "url": "url",
    "website": "url",
    "website url": "url",
    "title": "title",
    "task title": "title",
    "recipient": "recipient",
    "email recipient": "recipient",
    "subject": "subject",
    "email subject": "subject",
    "body": "body",
    "email body": "body",
    "description": "description",
}


def _explicit_send_requested(raw_text):
    text = raw_text or ""
    if _DEFERRED_SEND_PATTERN.search(text):
        return True

    for match in re.finditer(r"\b(?:send|sending|deliver|dispatch)\b", text, re.IGNORECASE):
        sentence_start = max(
            text.rfind(".", 0, match.start()),
            text.rfind("!", 0, match.start()),
            text.rfind("?", 0, match.start()),
        )
        prefix = text[sentence_start + 1 : match.start()]
        if not _NEGATED_SEND_PREFIX.search(prefix):
            return True
    return False


def _filter_missing_information(items, actions):
    filtered = []
    for item in items:
        label = re.sub(r"[_-]+", " ", item.strip().lower())
        known_tools = _ACTION_FIELDS.get(label)
        if known_tools is not None:
            relevant_actions = [
                action
                for action in actions
                if action["tool_to_use"] in known_tools
            ]
            field_key = _FIELD_KEYS.get(label)
            if field_key is None or not any(
                not action["parameters"].get(field_key, "").strip()
                for action in relevant_actions
            ):
                continue
        if item not in filtered:
            filtered.append(item)
    return filtered


def normalize_analysis(analysis, raw_text=""):
    """Validate model output and apply action-specific routing requirements."""
    if not isinstance(analysis, dict):
        raise ValueError("AI response is not a JSON object.")

    normalized = dict(analysis)
    normalized["task_title"] = str(
        normalized.get("task_title") or "Untitled task"
    )
    normalized["summary"] = str(normalized.get("summary") or "")
    normalized["priority"] = str(normalized.get("priority") or "Medium")
    if normalized["priority"] not in {"High", "Medium", "Low"}:
        normalized["priority"] = "Medium"

    missing = normalized.get("missing_information", [])
    if isinstance(missing, str):
        missing = [missing]
    if not isinstance(missing, list):
        missing = []
    missing = [str(item) for item in missing]

    actions = normalized.get("actions", [])
    if not isinstance(actions, list):
        actions = []

    normalized_actions = []
    for action in actions:
        if not isinstance(action, dict):
            continue

        category = action.get("routing_category", "requires_clarification")
        if not isinstance(category, str) or category not in VALID_CATEGORIES:
            category = "requires_clarification"

        tool = action.get("tool_to_use", "none")
        if not isinstance(tool, str) or tool not in VALID_TOOLS:
            tool = "none"

        raw_parameters = action.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raw_parameters = {}
        invalid_value = any(
            key in VALID_PARAMETER_KEYS and not isinstance(value, str)
            for key, value in raw_parameters.items()
        )
        parameters = {
            key: value
            for key, value in raw_parameters.items()
            if key in VALID_PARAMETER_KEYS and isinstance(value, str)
        }
        reason = str(action.get("reason") or "")

        if invalid_value:
            category = "requires_clarification"
            reason = "Action parameters must use text values."
        elif tool in {"draft_communication", "send_email"}:
            if raw_text.strip():
                tool = (
                    "send_email"
                    if _explicit_send_requested(raw_text)
                    else "draft_communication"
                )
            try:
                compose_email_message(
                    parameters.get("recipient", ""),
                    parameters.get("subject", ""),
                    parameters.get("body", ""),
                )
                parameters["recipient"] = normalize_recipients(
                    parameters.get("recipient", "")
                )
            except IntegrationError as exc:
                category = "requires_clarification"
                reason = exc.message
            else:
                if category != "cannot_execute_with_tools":
                    if tool == "send_email":
                        category = "prepare_for_human_review"
                    else:
                        category = "execute_automatically"

        elif tool == "create_trello_card":
            try:
                if not parameters.get("title", "").strip():
                    raise IntegrationError("validation", "Trello task title is required.")
                if not parameters.get("description", "").strip():
                    raise IntegrationError("validation", "Trello task description is required.")
                parameters["due_date"] = (
                    normalize_due_date(parameters.get("due_date")) or ""
                )
            except IntegrationError as exc:
                category = "requires_clarification"
                reason = exc.message
            else:
                if category not in {
                    "requires_clarification",
                    "cannot_execute_with_tools",
                }:
                    category = "prepare_for_human_review"

        if (
            tool in {"draft_communication", "send_email", "create_trello_card"}
            and category == "requires_clarification"
            and reason
        ):
            if reason not in missing:
                missing.append(reason)

        normalized_actions.append(
            {
                "action_title": str(action.get("action_title") or "Unnamed action"),
                "tool_to_use": tool,
                "routing_category": category,
                "reason": reason,
                "parameters": parameters,
            }
        )

    normalized["missing_information"] = _filter_missing_information(
        missing,
        normalized_actions,
    )
    normalized["actions"] = normalized_actions
    return normalized
