import hashlib
import hmac
import json
from datetime import datetime

from integrations.errors import IntegrationError


EXTERNAL_ACTIONS = {"send_email", "create_trello_card"}


def requires_human_approval(action_type, routing_category):
    if routing_category == "cannot_execute_with_tools":
        return False
    return (
        action_type in EXTERNAL_ACTIONS
        or routing_category == "prepare_for_human_review"
    )


def action_fingerprint(action_id, action_type, parameters):
    if isinstance(parameters, str):
        parameters = json.loads(parameters or "{}")
    if not isinstance(parameters, dict):
        raise ValueError("Action parameters must be a JSON object.")

    canonical = json.dumps(
        [action_id, action_type, parameters],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_activity(conn, event_type, action_id, status):
    conn.execute(
        """
        INSERT INTO activity_logs (timestamp, event_type, details)
        VALUES (?, ?, ?)
        """,
        (
            datetime.now().isoformat(),
            event_type,
            f"Action #{action_id} status changed to {status}.",
        ),
    )


def reject_pending_action(action_id, connection_factory):
    with connection_factory() as conn:
        cursor = conn.execute(
            """
            UPDATE actions
            SET status = 'REJECTED',
                output = 'Rejected by human operator.',
                updated_at = ?
            WHERE id = ?
              AND status = 'PENDING_APPROVAL'
            """,
            (datetime.now().isoformat(), action_id),
        )
        if cursor.rowcount != 1:
            return False
        _write_activity(conn, "HITL_REJECTED", action_id, "REJECTED")
        conn.commit()
    return True


def _execute_handler(action_type, parameters, send_email, create_trello_card, local_handlers):
    if action_type == "send_email":
        result = send_email(
            parameters["recipient"],
            parameters["subject"],
            parameters["body"],
        )
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise IntegrationError(
                "api",
                "Gmail did not return a confirmed message ID.",
                outcome_unknown=True,
            )
        return "gmail", message_id.strip(), None, {
            "status": "SENT",
            "message_id": message_id.strip(),
        }

    if action_type == "create_trello_card":
        result = create_trello_card(
            parameters["title"],
            parameters["description"],
            parameters.get("due_date"),
        )
        card_id = result.get("card_id") if isinstance(result, dict) else None
        card_url = result.get("card_url") if isinstance(result, dict) else None
        if not isinstance(card_id, str) or not card_id.strip():
            raise IntegrationError(
                "api",
                "Trello did not return a confirmed card ID.",
                outcome_unknown=True,
            )
        if not isinstance(card_url, str) or not card_url.strip():
            raise IntegrationError(
                "api",
                "Trello did not return a confirmed card URL.",
                outcome_unknown=True,
            )
        return "trello", card_id.strip(), card_url.strip(), {
            "status": "CREATED",
            "card_id": card_id.strip(),
            "card_url": card_url.strip(),
        }

    handler = local_handlers.get(action_type)
    if handler is None:
        raise ValueError("No handler is registered for this action.")
    result = handler(parameters)
    if isinstance(result, str):
        output = result
    else:
        output = json.dumps(result, ensure_ascii=False)
    return "local", None, None, output


def execute_approved_action(
    action_id,
    reviewed_fingerprint,
    connection_factory,
    *,
    send_email,
    create_trello_card,
    local_handlers,
):
    conn = connection_factory()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, action_type, routing_category, status, parameters
            FROM actions
            WHERE id = ?
            """,
            (action_id,),
        ).fetchone()
        if row is None or row["status"] != "PENDING_APPROVAL":
            conn.rollback()
            return False, "Action no longer awaits approval."
        if not isinstance(reviewed_fingerprint, str):
            conn.rollback()
            return False, "Action review confirmation is missing."

        try:
            parameters = json.loads(row["parameters"] or "{}")
            current_fingerprint = action_fingerprint(
                row["id"],
                row["action_type"],
                parameters,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            conn.rollback()
            return False, "Action parameters are invalid; submit a new proposal."

        if not hmac.compare_digest(current_fingerprint, reviewed_fingerprint):
            conn.rollback()
            return False, "Action parameters changed after review; review the current proposal."

        if (
            row["action_type"] in EXTERNAL_ACTIONS
            and row["routing_category"] != "prepare_for_human_review"
        ):
            conn.rollback()
            return False, "External actions must be explicitly prepared for review."
        if (
            row["action_type"] not in EXTERNAL_ACTIONS
            and row["action_type"] not in local_handlers
        ):
            conn.rollback()
            return False, "No supported handler is registered for this action."

        now = datetime.now().isoformat()
        cursor = conn.execute(
            """
            UPDATE actions
            SET status = 'EXECUTING',
                approval_hash = ?,
                approved_at = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'PENDING_APPROVAL'
            """,
            (current_fingerprint, now, now, action_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False, "Action no longer awaits approval."
        _write_activity(conn, "ACTION_EXECUTING", action_id, "EXECUTING")
        conn.commit()
    finally:
        conn.close()

    action_type = row["action_type"]
    service = (
        "gmail"
        if action_type == "send_email"
        else "trello"
        if action_type == "create_trello_card"
        else "local"
    )
    try:
        service_name, external_id, external_url, result = _execute_handler(
            action_type,
            parameters,
            send_email,
            create_trello_card,
            local_handlers,
        )
    except IntegrationError as exc:
        status = "OUTCOME_UNKNOWN" if exc.outcome_unknown else "FAILED"
        output = json.dumps({"status": status, "error": exc.message})
        with connection_factory() as conn:
            conn.execute(
                """
                UPDATE actions
                SET status = ?, output = ?, external_service = ?,
                    error_summary = ?, updated_at = ?, executed_at = ?
                WHERE id = ? AND status = 'EXECUTING'
                """,
                (
                    status,
                    output,
                    service,
                    exc.message,
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                    action_id,
                ),
            )
            _write_activity(conn, "ACTION_" + status, action_id, status)
            conn.commit()
        return False, output

    if isinstance(result, str):
        output = result
    else:
        output = json.dumps(result, ensure_ascii=False)
    now = datetime.now().isoformat()
    with connection_factory() as conn:
        cursor = conn.execute(
            """
            UPDATE actions
            SET status = 'COMPLETED', output = ?, external_service = ?,
                external_id = ?, external_url = ?, error_summary = NULL,
                updated_at = ?, executed_at = ?
            WHERE id = ? AND status = 'EXECUTING'
            """,
            (
                output,
                service_name,
                external_id,
                external_url,
                now,
                now,
                action_id,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return False, "The confirmed result could not be recorded; verify the external service."
        _write_activity(conn, "ACTION_COMPLETED", action_id, "COMPLETED")
        conn.commit()
    return True, output
