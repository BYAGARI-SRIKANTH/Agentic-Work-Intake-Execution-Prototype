import json
import sqlite3

from integrations.action_executor import (
    action_fingerprint,
    execute_approved_action,
    reject_pending_action,
    requires_human_approval,
)


def make_database(tmp_path):
    database = tmp_path / "actions.db"

    def connect():
        conn = sqlite3.connect(database, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY,
                request_id INTEGER,
                action_title TEXT,
                action_type TEXT,
                routing_category TEXT,
                status TEXT,
                output TEXT,
                parameters TEXT,
                approval_hash TEXT,
                approved_at TEXT,
                external_service TEXT,
                external_id TEXT,
                external_url TEXT,
                error_summary TEXT,
                created_at TEXT,
                updated_at TEXT,
                executed_at TEXT
            );
            CREATE TABLE activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                event_type TEXT,
                details TEXT
            );
            """
        )
        conn.execute(
            """
            INSERT INTO actions
            (id, request_id, action_title, action_type, routing_category,
             status, parameters, created_at, updated_at)
            VALUES (1, 1, 'Send update', 'send_email',
                    'prepare_for_human_review', 'PENDING_APPROVAL', ?, ?, ?)
            """,
            (
                json.dumps(
                    {
                        "recipient": "person@example.com",
                        "subject": "Status",
                        "body": "Ready",
                    }
                ),
                "now",
                "now",
            ),
        )
    return database, connect


def action_params(connection_factory):
    with connection_factory() as conn:
        row = conn.execute(
            "SELECT action_type, parameters FROM actions WHERE id = 1"
        ).fetchone()
        return row["action_type"], json.loads(row["parameters"])


def invoke(connection_factory, fingerprint, send_email, create_card=None):
    return execute_approved_action(
        1,
        fingerprint,
        connection_factory,
        send_email=send_email,
        create_trello_card=create_card or (lambda *args: {"args": args}),
        local_handlers={},
    )


def get_action(connection_factory):
    with connection_factory() as conn:
        return dict(conn.execute("SELECT * FROM actions WHERE id = 1").fetchone())


def test_rejected_action_is_never_executed(tmp_path):
    _, connect = make_database(tmp_path)
    assert reject_pending_action(1, connect)
    called = []

    ok, _ = invoke(
        connect,
        "reviewed-fingerprint",
        lambda *args: called.append(args),
    )

    assert not ok
    assert called == []
    assert get_action(connect)["status"] == "REJECTED"


def test_unapproved_action_is_never_executed(tmp_path):
    _, connect = make_database(tmp_path)
    called = []

    ok, _ = invoke(
        connect,
        "not-the-reviewed-fingerprint",
        lambda *args: called.append(args),
    )

    assert not ok
    assert called == []
    assert get_action(connect)["status"] == "PENDING_APPROVAL"


def test_modified_action_invalidates_reviewed_fingerprint(tmp_path):
    _, connect = make_database(tmp_path)
    action_type, parameters = action_params(connect)
    fingerprint = action_fingerprint(1, action_type, parameters)
    parameters["recipient"] = "attacker@example.com"
    with connect() as conn:
        conn.execute(
            "UPDATE actions SET parameters = ? WHERE id = 1",
            (json.dumps(parameters),),
        )
    called = []

    ok, message = invoke(connect, fingerprint, lambda *args: called.append(args))

    assert not ok
    assert "changed" in message
    assert called == []
    assert get_action(connect)["status"] == "PENDING_APPROVAL"


def test_modified_multi_recipient_list_invalidates_reviewed_fingerprint(tmp_path):
    _, connect = make_database(tmp_path)
    action_type, parameters = action_params(connect)
    parameters["recipient"] = "person@example.com, second@example.com"
    fingerprint = action_fingerprint(1, action_type, parameters)
    parameters["recipient"] = "person@example.com, third@example.com"
    with connect() as conn:
        conn.execute(
            "UPDATE actions SET parameters = ? WHERE id = 1",
            (json.dumps(parameters),),
        )
    called = []

    ok, message = invoke(connect, fingerprint, lambda *args: called.append(args))

    assert not ok
    assert "changed" in message
    assert called == []
    assert get_action(connect)["status"] == "PENDING_APPROVAL"


def test_repeated_approval_click_executes_only_once(tmp_path):
    _, connect = make_database(tmp_path)
    action_type, parameters = action_params(connect)
    fingerprint = action_fingerprint(1, action_type, parameters)
    called = []
    send = lambda *args: called.append(args) or {"message_id": "msg-1"}

    assert invoke(connect, fingerprint, send)[0]
    assert not invoke(connect, fingerprint, send)[0]
    assert called == [("person@example.com", "Status", "Ready")]
    assert get_action(connect)["status"] == "COMPLETED"


def test_external_failure_is_recorded_and_not_success(tmp_path):
    _, connect = make_database(tmp_path)
    action_type, parameters = action_params(connect)
    fingerprint = action_fingerprint(1, action_type, parameters)

    def fail(*args):
        from integrations.errors import IntegrationError

        assert args == ("person@example.com", "Status", "Ready")
        raise IntegrationError("api", "Gmail rejected the message.")

    assert not invoke(connect, fingerprint, fail)[0]
    action = get_action(connect)
    assert action["status"] == "FAILED"
    assert action["error_summary"] == "Gmail rejected the message."


def test_unknown_external_outcome_is_recorded_and_not_retried(tmp_path):
    _, connect = make_database(tmp_path)
    action_type, parameters = action_params(connect)
    fingerprint = action_fingerprint(1, action_type, parameters)
    calls = []

    def timeout(*args):
        from integrations.errors import IntegrationError

        calls.append(args)
        raise IntegrationError(
            "network",
            "Gmail did not confirm the email submission.",
            outcome_unknown=True,
        )

    assert not invoke(connect, fingerprint, timeout)[0]
    assert not invoke(connect, fingerprint, timeout)[0]
    action = get_action(connect)
    assert calls == [("person@example.com", "Status", "Ready")]
    assert action["status"] == "OUTCOME_UNKNOWN"


def test_action_is_executing_before_external_confirmation(tmp_path):
    database, connect = make_database(tmp_path)
    action_type, parameters = action_params(connect)
    fingerprint = action_fingerprint(1, action_type, parameters)
    observed_status = []

    def send(*args):
        assert args == ("person@example.com", "Status", "Ready")
        with sqlite3.connect(database) as observer:
            observed_status.append(
                observer.execute(
                    "SELECT status FROM actions WHERE id = 1"
                ).fetchone()[0]
            )
        return {"message_id": "confirmed-msg"}

    assert invoke(connect, fingerprint, send)[0]
    assert observed_status == ["EXECUTING"]
    action = get_action(connect)
    assert action["status"] == "COMPLETED"
    assert action["external_id"] == "confirmed-msg"


def test_external_actions_always_require_approval():
    assert requires_human_approval("send_email", "execute_automatically")
    assert requires_human_approval("create_trello_card", "execute_automatically")
    assert not requires_human_approval(
        "send_email",
        "cannot_execute_with_tools",
    )
    assert not requires_human_approval(
        "create_trello_card",
        "cannot_execute_with_tools",
    )
    assert not requires_human_approval("check_website_status", "execute_automatically")


def test_existing_local_draft_workflow_remains_local(tmp_path):
    _, connect = make_database(tmp_path)
    with connect() as conn:
        conn.execute(
            """
            UPDATE actions
            SET action_type = 'draft_communication',
                parameters = ?
            WHERE id = 1
            """,
            (
                json.dumps(
                    {
                        "recipient": "person@example.com",
                        "subject": "Status",
                        "body": "Draft only",
                    }
                ),
            ),
        )
    action_type, parameters = action_params(connect)
    fingerprint = action_fingerprint(1, action_type, parameters)
    called = []

    ok, output = execute_approved_action(
        1,
        fingerprint,
        connect,
        send_email=lambda *args: called.append(("gmail", args)),
        create_trello_card=lambda *args: called.append(("trello", args)),
        local_handlers={
            "draft_communication": lambda params: (
                json.dumps(
                    {"status": "DRAFT_SAVED", "note": "No email was sent."}
                )
                if params["body"] == "Draft only"
                else ""
            )
        },
    )

    assert ok
    assert json.loads(output)["status"] == "DRAFT_SAVED"
    assert called == []
    assert get_action(connect)["external_service"] == "local"
