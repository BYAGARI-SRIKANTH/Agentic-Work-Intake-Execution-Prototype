import os
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from dotenv import load_dotenv
from groq import Groq

from integrations.action_executor import (
    action_fingerprint,
    execute_approved_action,
    reject_pending_action,
    requires_human_approval,
)
from integrations.gmail_service import send_email
from integrations.intake_routing import normalize_analysis
from integrations.safe_web import check_public_website
from integrations.trello_service import create_trello_card


load_dotenv(Path(__file__).resolve().parent / ".env")


# =============================================================================
# CONFIGURATION
# =============================================================================

st.set_page_config(
    page_title="Work Intake Agent Prototype",
    page_icon="⚡",
    layout="wide",
)

DB_NAME = "agent_system.db"

# Use Groq production model IDs. The fallback helps when the primary model is
# temporarily unavailable or rate-limited.
PRIMARY_MODEL = "openai/gpt-oss-20b"
FALLBACK_MODELS = [
    "openai/gpt-oss-120b",
]

# =============================================================================
# API KEY / GROQ
# =============================================================================

def get_api_key():
    """Load the Groq key from Streamlit Secrets or environment variables."""
    try:
        key = st.secrets.get("GROQ_API_KEY")
    except StreamlitSecretNotFoundError:
        key = None

    key = key or os.getenv("GROQ_API_KEY")
    if key:
        return key.strip()

    return ""


def get_integration_setting(name, default=""):
    try:
        value = st.secrets.get(name)
    except StreamlitSecretNotFoundError:
        value = None
    return str(value or os.getenv(name, default)).strip()


def is_rate_limit_error(exc):
    """True when Groq reports a rate limit or usage quota error."""
    message = str(exc).upper()
    rate_limit_markers = (
        "429",
        "RATE_LIMIT",
        "RATE LIMIT",
        "QUOTA EXCEEDED",
        "USAGE LIMIT",
    )
    return any(marker in message for marker in rate_limit_markers)


def is_retryable_error(exc):
    """Detect transient service/rate-limit failures, not permanent code errors."""
    message = str(exc).upper()
    return any(
        marker in message
        for marker in (
            "429",
            "500",
            "502",
            "503",
            "504",
            "TIMEOUT",
            "CONNECTION",
            "TEMPORARILY",
            "OVERLOADED",
        )
    )


def call_groq(prompt):
    """Call Groq with structured output, retries, and fallback models."""
    api_key = get_api_key()
    if not api_key:
        raise RuntimeError(
            "Groq API key is not configured. Add GROQ_API_KEY "
            "to Streamlit Cloud → Manage app → Settings → Secrets."
        )

    client = Groq(api_key=api_key)

    errors = []
    models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
                response_text = response.choices[0].message.content
                if not response_text:
                    raise RuntimeError(f"Groq returned an empty response using {model_name}.")

                result = json.loads(response_text)
                result.setdefault("task_title", "Untitled task")
                result.setdefault("summary", "")
                result.setdefault("priority", "Medium")
                result.setdefault("detected_deadline", "None detected")
                result.setdefault("missing_information", [])
                result.setdefault("actions", [])
                result["_model_used"] = model_name
                return result

            except Exception as exc:
                errors.append(f"{model_name}: {exc}")

                if is_retryable_error(exc) and attempt == 0:
                    time.sleep(2)
                    continue
                break

    if errors and all(is_rate_limit_error(err) for err in errors):
        raise RuntimeError(
            "Groq rate limits or usage quotas are preventing requests for the "
            "configured models. Check your Groq Console limits and try again."
        )

    raise RuntimeError(
        "Groq could not process this request with the configured models. "
    )


# =============================================================================
# DATABASE
# =============================================================================

def get_db_connection():
    """SQLite connection with WAL and a reasonable timeout."""
    conn = sqlite3.connect(
        DB_NAME,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    # WAL improves concurrent read/write behavior.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def add_column_if_missing(conn, table_name, column_name, column_definition):
    """Safely add a column to an existing database."""
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }

    if column_name not in columns:
        conn.execute(
            f"ALTER TABLE {table_name} ADD COLUMN "
            f"{column_name} {column_definition}"
        )


def init_db():
    """
    Create the current schema and migrate older versions.

    This specifically fixes the old deployment error:
    'table requests has no column named created_at'
    """
    with get_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_text TEXT NOT NULL,
                structured_json TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                action_title TEXT,
                action_type TEXT,
                routing_category TEXT,
                reason TEXT,
                status TEXT,
                output TEXT,
                parameters TEXT,
                created_at DATETIME,
                updated_at DATETIME,
                FOREIGN KEY(request_id) REFERENCES requests(id)
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                event_type TEXT,
                details TEXT
            )
            """
        )

        # ---------------------------------------------------------------------
        # Migrate the database created by your older app.py.
        # ---------------------------------------------------------------------
        add_column_if_missing(
            conn, "requests", "created_at", "DATETIME"
        )

        add_column_if_missing(
            conn, "actions", "parameters", "TEXT"
        )

        add_column_if_missing(
            conn, "actions", "created_at", "DATETIME"
        )

        add_column_if_missing(
            conn, "actions", "updated_at", "DATETIME"
        )
        add_column_if_missing(
            conn, "actions", "approval_hash", "TEXT"
        )
        add_column_if_missing(
            conn, "actions", "approved_at", "DATETIME"
        )
        add_column_if_missing(
            conn, "actions", "executed_at", "DATETIME"
        )
        add_column_if_missing(
            conn, "actions", "external_service", "TEXT"
        )
        add_column_if_missing(
            conn, "actions", "external_id", "TEXT"
        )
        add_column_if_missing(
            conn, "actions", "external_url", "TEXT"
        )
        add_column_if_missing(
            conn, "actions", "error_summary", "TEXT"
        )

        # Backfill created_at from the old timestamp column.
        conn.execute(
            """
            UPDATE requests
            SET created_at = COALESCE(created_at, timestamp, CURRENT_TIMESTAMP)
            WHERE created_at IS NULL
            """
        )

        conn.execute(
            """
            UPDATE actions
            SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP)
            WHERE created_at IS NULL
            """
        )

        conn.execute(
            """
            UPDATE actions
            SET updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)
            WHERE updated_at IS NULL
            """
        )

        conn.commit()


def log_activity(event_type, details, conn=None):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if conn is not None:
        conn.execute(
            """
            INSERT INTO activity_logs
            (timestamp, event_type, details)
            VALUES (?, ?, ?)
            """,
            (timestamp, event_type, str(details)),
        )
        return

    with get_db_connection() as db:
        db.execute(
            """
            INSERT INTO activity_logs
            (timestamp, event_type, details)
            VALUES (?, ?, ?)
            """,
            (timestamp, event_type, str(details)),
        )
        db.commit()


# Initialize database BEFORE the UI queries it.
init_db()


# =============================================================================
# AGENT TOOLS
# =============================================================================

def check_website_status(url):
    """Check a public HTTP(S) website while pinning DNS and validating redirects."""
    if not url:
        return json.dumps(
            {
                "reachable": False,
                "error": "No URL provided.",
            },
            indent=2,
        )

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    return json.dumps(check_public_website(url), indent=2)


def create_task_record(title, deadline, priority):
    """
    Simulated task creation.
    No external calendar/task system is contacted.
    """
    return json.dumps(
        {
            "status": "CREATED",
            "task_title": title or "Untitled task",
            "deadline": deadline or "Not specified",
            "priority": priority or "Medium",
            "saved_at": datetime.now().isoformat(),
            "note": "Prototype task record; no external task system was contacted.",
        },
        indent=2,
    )


def draft_communication(recipient, subject, body):
    """
    Draft only. Never sends an external email.
    """
    return json.dumps(
        {
            "status": "DRAFT_SAVED",
            "recipient": recipient or "MISSING_RECIPIENT",
            "subject": subject or "No subject",
            "body": body or "",
            "note": "Draft only; no email was sent.",
        },
        indent=2,
    )


# =============================================================================
# LLM PROMPT
# =============================================================================

SYSTEM_PROMPT = """
You are an Agentic Work Intake Engine.

Your job is to convert unstructured business requests into a safe,
structured, reviewable execution plan.

IMPORTANT SAFETY RULES:
- Never invent missing recipients, documents, deadlines, URLs, or meeting details.
- If vital information is missing, use requires_clarification.
- Do not claim an external email was sent.
- Do not claim a real calendar reminder was created.
- Only use check_website_status for a bounded website check.
- create_task_record requires human approval; draft_communication creates only
  a local draft and does not require approval to avoid sending.
- send_email and create_trello_card always require explicit human approval.
- Use draft_communication only when the user wants an email prepared or shown,
  without asking for it to be sent later.
- If the user says not to send until they approve, or asks to send after
  approval, use send_email and route it to prepare_for_human_review.
- Preparing a draft alone never sends email. Sending is a separate action that
  can happen only after the user approves the exact proposed message.
- A local task record is not a Trello card; a communication draft is not an email.
- Never choose execute_automatically for an email send or Trello card.
- Keep the plan small and practical.

Every action MUST use exactly one routing category:

1. execute_automatically
   Safe, bounded work that can run immediately.
   Example: checking whether a website is reachable.

2. prepare_for_human_review
   Work that creates a draft/task record or otherwise should wait for a
   human decision before completion.

3. cannot_execute_with_tools
   A valid request that cannot be performed by the available prototype tools.

4. requires_clarification
   Required information is missing. Do not guess it.

Available tools:

- check_website_status
- create_task_record
- draft_communication (local draft only; no sending)
- send_email (recipient, subject, and plain-text body are required)
- create_trello_card (title and description are required; due_date is optional)

Only include the parameters needed by the selected action. Never list
irrelevant fields as missing information. For example, email actions need only
recipient, subject, and body; website checks need only url; Trello cards need
title and description, with due_date optional.

Return ONLY valid JSON with task_title, summary, priority, detected_deadline,
missing_information, and actions. Each action has action_title, tool_to_use,
routing_category, reason, and parameters. The parameters object may contain
only fields relevant to that action.
"""


def process_intake(text):
    prompt = (
        SYSTEM_PROMPT
        + "\n\nIncoming unstructured work:\n"
        + text
    )
    return call_groq(prompt)


# =============================================================================
# DATABASE OPERATIONS
# =============================================================================

def save_request_and_actions(raw_text, analysis):
    now = datetime.now().isoformat()

    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO requests
            (raw_text, structured_json, timestamp, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                raw_text,
                json.dumps(analysis),
                now,
                now,
            ),
        )

        request_id = cursor.lastrowid

        for action in analysis["actions"]:
            category = action["routing_category"]
            tool = action["tool_to_use"]
            params = action["parameters"]

            if tool == "none":
                category = "cannot_execute_with_tools"
                output = "No supported action was selected."
                status = "FLAGGED"
                log_activity(
                    "ROUTED_FLAGGED",
                    f"{action['action_title']} has no supported action.",
                    conn,
                )

            elif category == "cannot_execute_with_tools":
                output = action["reason"] or (
                    "This action cannot be performed with the available tools."
                )
                status = "FLAGGED"
                log_activity(
                    "ROUTED_FLAGGED",
                    f"{action['action_title']} cannot be performed with available tools.",
                    conn,
                )

            elif tool in {"send_email", "create_trello_card"} and category == "requires_clarification":
                output = action["reason"] or "Required action information is missing."
                status = "NEEDS_CLARIFICATION"
                log_activity(
                    "ROUTED_CLARIFICATION",
                    f"{action['action_title']} needs clarification.",
                    conn,
                )

            elif tool in {"send_email", "create_trello_card"} and category != "cannot_execute_with_tools":
                category = "prepare_for_human_review"
                output = (
                    "External action is awaiting explicit human approval. "
                    "No external request has been made."
                )
                status = "PENDING_APPROVAL"
                log_activity(
                    "ROUTED_HITL",
                    f"{action['action_title']} paused for human approval.",
                    conn,
                )

            elif category == "execute_automatically":
                if tool == "check_website_status":
                    output = check_website_status(
                        params.get("url", "")
                    )
                    status = "COMPLETED"

                elif tool == "draft_communication":
                    output = draft_communication(
                        params.get("recipient", ""),
                        params.get("subject", ""),
                        params.get("body", ""),
                    )
                    status = "COMPLETED"

                else:
                    output = (
                        "The requested automatic tool is not available."
                    )
                    status = "FLAGGED"

                log_activity(
                    "AUTO_EXECUTED",
                    f"{action['action_title']} | Tool: {tool}",
                    conn,
                )

            elif requires_human_approval(tool, category):
                category = "prepare_for_human_review"
                output = (
                    "Waiting for human approval in the "
                    "Pending Human Approvals tab."
                )
                status = "PENDING_APPROVAL"

                log_activity(
                    "ROUTED_HITL",
                    f"{action['action_title']} paused for human approval.",
                    conn,
                )

            elif category == "requires_clarification":
                output = (
                    f"Additional information is required. "
                    f"Reason: {action['reason']}"
                )
                status = "NEEDS_CLARIFICATION"
                log_activity(
                    "ROUTED_CLARIFICATION",
                    f"{action['action_title']} needs clarification.",
                    conn,
                )

            else:
                output = (
                    f"Action flagged as {category}. "
                    f"Reason: {action['reason']}"
                )
                status = "FLAGGED"

                log_activity(
                    "ROUTED_FLAGGED",
                    f"{action['action_title']} -> {category}",
                    conn,
                )

            cursor.execute(
                """
                INSERT INTO actions
                (
                    request_id,
                    action_title,
                    action_type,
                    routing_category,
                    reason,
                    status,
                    output,
                    parameters,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    action["action_title"],
                    tool,
                    category,
                    action["reason"],
                    status,
                    output,
                    json.dumps(params),
                    now,
                    now,
                ),
            )

        conn.commit()

    return request_id


def get_pending_approvals():
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT
                a.id,
                a.request_id,
                a.action_title,
                a.action_type,
                a.routing_category,
                a.reason,
                a.status,
                a.output,
                a.parameters,
                a.created_at,
                r.raw_text
            FROM actions a
            JOIN requests r ON r.id = a.request_id
            WHERE a.status = 'PENDING_APPROVAL'
            ORDER BY a.id DESC
            """
        ).fetchall()


def get_history():
    with get_db_connection() as conn:
        return conn.execute(
            """
            SELECT
                id,
                raw_text,
                structured_json,
                timestamp,
                created_at
            FROM requests
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()


def approve_action(action_id, reviewed_fingerprint):
    return execute_approved_action(
        action_id,
        reviewed_fingerprint,
        get_db_connection,
        send_email=lambda recipient, subject, body: send_email(
            recipient,
            subject,
            body,
            token_file=get_integration_setting("GMAIL_TOKEN_FILE", "token.json"),
            oauth_client_id=get_integration_setting("GMAIL_OAUTH_CLIENT_ID"),
            oauth_client_secret=get_integration_setting(
                "GMAIL_OAUTH_CLIENT_SECRET"
            ),
            refresh_token=get_integration_setting("GMAIL_REFRESH_TOKEN"),
        ),
        create_trello_card=lambda title, description, due_date: create_trello_card(
            title,
            description,
            due_date,
            api_key=get_integration_setting("TRELLO_API_KEY"),
            token=get_integration_setting("TRELLO_TOKEN"),
            list_id=get_integration_setting("TRELLO_LIST_ID"),
        ),
        local_handlers={
            "draft_communication": lambda params: draft_communication(
                params.get("recipient", ""),
                params.get("subject", ""),
                params.get("body", ""),
            ),
            "create_task_record": lambda params: create_task_record(
                params.get("title", ""),
                params.get("deadline", ""),
                params.get("priority", ""),
            ),
        },
    )


def reject_action(action_id):
    return reject_pending_action(action_id, get_db_connection)


# =============================================================================
# SIDEBAR
# =============================================================================

with st.sidebar:
    st.header("⚙️ Settings & Key")

    api_key = get_api_key()

    if api_key:
        st.success("Groq API key loaded.")
    else:
        st.error(
            "Groq API key is not configured.\n\n"
            "Add GROQ_API_KEY to a .env file or Streamlit Cloud "
            "→ Manage app → Settings → Secrets."
        )

    st.markdown("---")

    st.header("🤖 AI Configuration")

    st.write(f"**Primary model:** `{PRIMARY_MODEL}`")
    st.write(
        "**Automatic fallback:** "
        "`openai/gpt-oss-120b`"
    )
    st.caption("Groq API")

    st.markdown("---")

    st.header("📜 Live Activity Trace")

    with get_db_connection() as conn:
        logs = conn.execute(
            """
            SELECT timestamp, event_type, details
            FROM activity_logs
            ORDER BY id DESC
            LIMIT 12
            """
        ).fetchall()

    if not logs:
        st.caption("No activity yet.")

    for log in logs:
        st.caption(
            f"**[{log['timestamp']}] {log['event_type']}**\n"
            f"{log['details']}"
        )


# =============================================================================
# MAIN UI
# =============================================================================

st.title("⚡ Agentic Work Intake & Execution Prototype")
st.caption("Altibbe Practical AI Engineering Assignment Submission")

tab_intake, tab_pending, tab_history = st.tabs(
    [
        "📥 Intake & Processing",
        "⏸️ Pending Human Approvals",
        "📊 Database History",
    ]
)


# =============================================================================
# TAB 1 — INTAKE
# =============================================================================

with tab_intake:
    st.subheader("1. Submit Unstructured Work")

    user_text = st.text_area(
        "Paste email, meeting notes, customer request, or founder instruction:",
        height=180,
        placeholder=(
            "Example: Please review hedamo.com uptime and draft "
            "a summary email to the partner."
        ),
    )

    if st.button(
        "🚀 Submit to Agent",
        type="primary",
        use_container_width=False,
    ):
        if not api_key:
            st.error(
                "Groq API key is missing. Configure GROQ_API_KEY "
                "in a .env file or Streamlit Secrets."
            )

        elif not user_text.strip():
            st.warning("Please provide unstructured text input.")

        else:
            log_activity(
                "INTAKE_RECEIVED",
                "A new work request was received.",
            )

            with st.spinner(
                "AI Engine analyzing, planning and routing..."
            ):
                try:
                    analysis = process_intake(user_text)
                    analysis = normalize_analysis(
                        analysis,
                        raw_text=user_text,
                    )

                    request_id = save_request_and_actions(
                        user_text,
                        analysis,
                    )

                    log_activity(
                        "WORK_COMPLETED",
                        f"Request #{request_id} processed successfully.",
                    )

                    st.success(
                        f"Work processed and routed successfully! "
                        f"Request ID: #{request_id}"
                    )

                    col1, col2 = st.columns(2)

                    with col1:
                        st.markdown("### Agent Analysis")
                        st.write(
                            f"**Task:** {analysis['task_title']}"
                        )
                        st.write(
                            f"**Priority:** {analysis['priority']}"
                        )
                        st.write(
                            f"**Summary:** {analysis['summary']}"
                        )

                        if analysis["missing_information"]:
                            st.warning("Missing information:")
                            for item in analysis["missing_information"]:
                                st.write(f"- {item}")

                    with col2:
                        st.markdown("### Execution Plan")

                        for index, action in enumerate(
                            analysis["actions"], start=1
                        ):
                            category = action["routing_category"]

                            if category == "execute_automatically":
                                icon = "🟢"
                            elif category == "prepare_for_human_review":
                                icon = "🟡"
                            elif category == "requires_clarification":
                                icon = "🟠"
                            else:
                                icon = "🔴"

                            st.write(
                                f"{icon} **{index}. "
                                f"{action['action_title']}**"
                            )
                            st.caption(
                                f"Tool: `{action['tool_to_use']}` | "
                                f"Route: `{category}`"
                            )
                            if action["tool_to_use"] in {
                                "draft_communication",
                                "send_email",
                            }:
                                email = action["parameters"]
                                st.markdown("**Proposed email:**")
                                st.write(
                                    f"**Recipient:** {email.get('recipient', '')}"
                                )
                                st.write(
                                    f"**Subject:** {email.get('subject', '')}"
                                )
                                st.text_area(
                                    "Plain-text body",
                                    value=email.get("body", ""),
                                    height=140,
                                    disabled=True,
                                    key=f"intake_email_body_{request_id}_{index}",
                                )
                                if action["tool_to_use"] == "draft_communication":
                                    st.caption(
                                        "Local draft only; no email was sent."
                                    )
                                else:
                                    st.caption(
                                        "Gmail will only send this after explicit approval."
                                    )

                    with st.expander("View complete structured JSON"):
                        st.json(analysis)

                except Exception as exc:
                    log_activity(
                        "FAILURE_PATH",
                        f"Request processing failed ({type(exc).__name__}).",
                    )

                    st.error(
                        "The request could not be processed. No success "
                        "was recorded; check the application configuration."
                    )

                    if is_rate_limit_error(exc):
                        st.warning(
                            "Groq rate limits or usage quotas are preventing requests. "
                            "Check your limits in the Groq Console before trying again."
                        )
                    elif is_retryable_error(exc):
                        st.info(
                            "Groq is temporarily rate-limited or unavailable. "
                            "The app tried its configured fallbacks. Please try again later."
                        )


# =============================================================================
# TAB 2 — HUMAN APPROVAL
# =============================================================================

with tab_pending:
    st.subheader("2. Human-in-the-Loop Approval Queue")

    pending_actions = get_pending_approvals()

    if not pending_actions:
        st.info("No actions currently waiting for approval.")

    else:
        for action in pending_actions:
            action_id = action["id"]

            with st.expander(
                f"🔴 Pending #{action_id}: "
                f"{action['action_title']}",
                expanded=True,
            ):
                st.write(
                    f"**Tool:** `{action['action_type']}`"
                )
                st.write(
                    f"**Reason:** {action['reason']}"
                )
                st.write(
                    f"**Created:** {action['created_at']}"
                )
                st.markdown("**Original work request:**")
                st.write(action["raw_text"])

                parameters_valid = True
                try:
                    params = json.loads(
                        action["parameters"] or "{}"
                    )
                    if not isinstance(params, dict):
                        raise ValueError("Action parameters must be an object.")
                except (json.JSONDecodeError, ValueError):
                    params = {}
                    parameters_valid = False
                    st.error("The saved action parameters are invalid; it cannot be approved.")

                action_type = action["action_type"]
                if parameters_valid and action_type == "send_email":
                    st.markdown("**Email to be sent (review these exact details):**")
                    st.write(f"**Recipients:** {params.get('recipient', '')}")
                    st.write(f"**Subject:** {params.get('subject', '')}")
                    st.text_area(
                        "Plain-text body",
                        value=params.get("body", ""),
                        height=180,
                        disabled=True,
                        key=f"review_email_body_{action_id}",
                    )
                elif parameters_valid and action_type == "create_trello_card":
                    st.markdown("**Trello card to be created (review these exact details):**")
                    st.write(f"**Title:** {params.get('title', '')}")
                    st.text_area(
                        "Description",
                        value=params.get("description", ""),
                        height=150,
                        disabled=True,
                        key=f"review_trello_description_{action_id}",
                    )
                    if params.get("due_date"):
                        st.write(f"**Due:** {params['due_date']}")
                elif parameters_valid:
                    st.markdown("**Proposed local action parameters:**")
                    st.json(params)

                try:
                    reviewed_fingerprint = action_fingerprint(
                        action_id,
                        action_type,
                        params,
                    )
                except (TypeError, ValueError):
                    reviewed_fingerprint = None

                st.markdown("**Current status:**")
                st.info(action["status"])

                col1, col2 = st.columns(2)

                with col1:
                    if st.button(
                        "✅ Approve & Execute",
                        key=f"approve_{action_id}",
                        use_container_width=True,
                        disabled=not parameters_valid or reviewed_fingerprint is None,
                    ):
                        ok, result = approve_action(
                            action_id,
                            reviewed_fingerprint,
                        )
                        try:
                            result_data = json.loads(result)
                        except (json.JSONDecodeError, TypeError):
                            result_data = {"message": result}
                        if not isinstance(result_data, dict):
                            result_data = {"message": result_data}

                        if ok:
                            if action_type == "send_email":
                                st.success("Gmail confirmed the email submission.")
                                st.write(
                                    f"**Gmail message ID:** "
                                    f"`{result_data['message_id']}`"
                                )
                            elif action_type == "create_trello_card":
                                st.success("Trello confirmed card creation.")
                                st.markdown(
                                    f"[Open Trello card]({result_data['card_url']})"
                                )
                                st.write(
                                    f"**Trello card ID:** `{result_data['card_id']}`"
                                )
                            else:
                                st.success(
                                    "Local action completed. No external service was contacted."
                                )
                                st.json(result_data)
                        else:
                            status = result_data.get("status")
                            if status == "OUTCOME_UNKNOWN":
                                st.warning(
                                    "The external service may have performed this action, "
                                    "but did not confirm it. Check the service manually; "
                                    "this action will not be retried automatically."
                                )
                                st.error(result_data.get("error", "Outcome unknown."))
                            elif status == "FAILED":
                                st.error(
                                    result_data.get("error", "The action failed.")
                                )
                            else:
                                st.error(result_data.get("message", result))

                with col2:
                    if st.button(
                        "❌ Reject",
                        key=f"reject_{action_id}",
                        use_container_width=True,
                    ):
                        if reject_action(action_id):
                            st.warning(
                                "Action rejected by human operator."
                            )
                        else:
                            st.error(
                                "Action could not be rejected."
                            )


# =============================================================================
# TAB 3 — HISTORY
# =============================================================================

with tab_history:
    st.subheader("3. Persistent Storage — SQLite")

    history = get_history()

    if not history:
        st.info("No requests stored yet.")

    else:
        for request in history:
            request_id = request["id"]

            with st.expander(
                f"Request #{request_id} — "
                f"{request['created_at'] or request['timestamp']}"
            ):
                st.markdown("**Original request:**")
                st.write(request["raw_text"])

                try:
                    structured = json.loads(
                        request["structured_json"] or "{}"
                    )
                except json.JSONDecodeError:
                    structured = {}

                st.markdown("**Structured interpretation:**")
                st.json(structured)

                with get_db_connection() as conn:
                    actions = conn.execute(
                        """
                        SELECT
                            action_title,
                            action_type,
                            routing_category,
                            reason,
                            status,
                            output,
                            external_service,
                            external_id,
                            external_url,
                            error_summary,
                            created_at,
                            updated_at
                        FROM actions
                        WHERE request_id = ?
                        ORDER BY id
                        """,
                        (request_id,),
                    ).fetchall()

                st.markdown("**Actions:**")

                if actions:
                    for action in actions:
                        st.write(
                            f"**{action['action_title']}** — "
                            f"`{action['status']}`"
                        )
                        st.caption(
                            f"Tool: `{action['action_type']}` | "
                            f"Route: `{action['routing_category']}`"
                        )

                        if action["output"]:
                            st.code(
                                action["output"],
                                language="json",
                            )
                        if action["external_id"]:
                            st.write(
                                f"**Confirmed {action['external_service']} ID:** "
                                f"`{action['external_id']}`"
                            )
                        if action["external_url"]:
                            st.markdown(
                                f"[Open external action]({action['external_url']})"
                            )
                        if action["error_summary"]:
                            st.warning(action["error_summary"])

                        st.divider()
                else:
                    st.caption("No actions stored.")


# =============================================================================
# FOOTER
# =============================================================================

st.markdown("---")
st.caption(
    f"Powered by {PRIMARY_MODEL} • SQLite persistence • "
    "Human-in-the-loop approvals • Bounded tool execution"
)