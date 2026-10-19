import os
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv
from groq import Groq


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
        if key:
            return str(key).strip()
    except Exception:
        pass

    key = os.getenv("GROQ_API_KEY")
    if key:
        return key.strip()

    return ""


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

    schema = {
        "type": "object",
        "properties": {
            "task_title": {"type": "string"},
            "summary": {"type": "string"},
            "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
            "detected_deadline": {"type": "string"},
            "missing_information": {"type": "array", "items": {"type": "string"}},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action_title": {"type": "string"},
                        "tool_to_use": {
                            "type": "string",
                            "enum": ["check_website_status", "create_task_record", "draft_communication", "none"],
                        },
                        "routing_category": {
                            "type": "string",
                            "enum": ["execute_automatically", "prepare_for_human_review", "cannot_execute_with_tools", "requires_clarification"],
                        },
                        "reason": {"type": "string"},
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "url": {"type": "string"},
                                "title": {"type": "string"},
                                "deadline": {"type": "string"},
                                "priority": {"type": "string"},
                                "recipient": {"type": "string"},
                                "subject": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": [
                                "url",
                                "title",
                                "deadline",
                                "priority",
                                "recipient",
                                "subject",
                                "body",
                            ],
                            "additionalProperties": False,
                        },
                    },
                    "required": [
                        "action_title",
                        "tool_to_use",
                        "routing_category",
                        "reason",
                        "parameters",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "task_title",
            "summary",
            "priority",
            "detected_deadline",
            "missing_information",
            "actions",
        ],
        "additionalProperties": False,
    }

    errors = []
    models_to_try = [PRIMARY_MODEL] + FALLBACK_MODELS

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "work_intake_analysis",
                            "strict": True,
                            "schema": schema,
                        },
                    },
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
        f"Last error: {errors[-1] if errors else 'Unknown error'}"
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
    """Real bounded website availability check."""
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

    try:
        started = time.perf_counter()

        response = requests.get(
            url,
            timeout=8,
            allow_redirects=True,
            headers={
                "User-Agent": "Altibbe-Agent-Prototype/1.0"
            },
        )

        elapsed = round(time.perf_counter() - started, 3)

        return json.dumps(
            {
                "url": url,
                "final_url": response.url,
                "status_code": response.status_code,
                "reachable": response.status_code < 500,
                "response_time_seconds": elapsed,
            },
            indent=2,
        )

    except requests.RequestException as exc:
        return json.dumps(
            {
                "url": url,
                "reachable": False,
                "error": str(exc),
            },
            indent=2,
        )


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
- create_task_record and draft_communication require human approval.
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
- draft_communication

Return ONLY JSON matching the supplied schema.
"""


def process_intake(text):
    prompt = (
        SYSTEM_PROMPT
        + "\n\nIncoming unstructured work:\n"
        + text
    )
    return call_groq(prompt)


# =============================================================================
# VALIDATION / NORMALIZATION
# =============================================================================

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
    "none",
}


def normalize_analysis(analysis):
    """Protect the app if the model returns unexpected values."""
    if not isinstance(analysis, dict):
        raise ValueError("AI response is not a JSON object.")

    analysis["task_title"] = str(
        analysis.get("task_title") or "Untitled task"
    )

    analysis["summary"] = str(
        analysis.get("summary") or ""
    )

    analysis["priority"] = str(
        analysis.get("priority") or "Medium"
    )

    if analysis["priority"] not in {"High", "Medium", "Low"}:
        analysis["priority"] = "Medium"

    missing = analysis.get("missing_information", [])
    if isinstance(missing, str):
        missing = [missing]
    if not isinstance(missing, list):
        missing = []

    analysis["missing_information"] = [
        str(item) for item in missing
    ]

    actions = analysis.get("actions", [])
    if not isinstance(actions, list):
        actions = []

    normalized_actions = []

    for action in actions:
        if not isinstance(action, dict):
            continue

        category = action.get("routing_category", "requires_clarification")
        tool = action.get("tool_to_use", "none")

        if category not in VALID_CATEGORIES:
            category = "requires_clarification"

        if tool not in VALID_TOOLS:
            tool = "none"

        params = action.get("parameters", {})
        if not isinstance(params, dict):
            params = {}

        normalized_actions.append(
            {
                "action_title": str(
                    action.get("action_title") or "Unnamed action"
                ),
                "tool_to_use": tool,
                "routing_category": category,
                "reason": str(action.get("reason") or ""),
                "parameters": params,
            }
        )

    analysis["actions"] = normalized_actions
    return analysis


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

            if category == "execute_automatically":
                if tool == "check_website_status":
                    output = check_website_status(
                        params.get("url", "")
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

            elif category == "prepare_for_human_review":
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
                a.created_at
            FROM actions a
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


def approve_action(action_id):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT
                id,
                action_title,
                action_type,
                parameters
            FROM actions
            WHERE id = ?
              AND status = 'PENDING_APPROVAL'
            """,
            (action_id,),
        ).fetchone()

        if row is None:
            return False, "Action no longer exists or was already processed."

        try:
            params = json.loads(row["parameters"] or "{}")
        except json.JSONDecodeError:
            params = {}

        tool = row["action_type"]

        if tool == "draft_communication":
            result = draft_communication(
                params.get("recipient", ""),
                params.get("subject", row["action_title"]),
                params.get("body", ""),
            )

        elif tool == "create_task_record":
            result = create_task_record(
                params.get("title", row["action_title"]),
                params.get("deadline", "Not specified"),
                params.get("priority", "Medium"),
            )

        else:
            result = (
                "Approved, but no supported human-review tool "
                "was associated with this action."
            )

        now = datetime.now().isoformat()

        conn.execute(
            """
            UPDATE actions
            SET status = 'APPROVED_AND_EXECUTED',
                output = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (result, now, action_id),
        )

        log_activity(
            "HITL_APPROVED",
            f"Action #{action_id} approved and executed as a prototype action.",
            conn,
        )

        conn.commit()

    return True, result


def reject_action(action_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
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

        if cursor.rowcount == 0:
            return False

        log_activity(
            "HITL_REJECTED",
            f"Action #{action_id} rejected by human operator.",
            conn,
        )

        conn.commit()

    return True


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
                f"Text snippet: {user_text[:120]}...",
            )

            with st.spinner(
                "AI Engine analyzing, planning and routing..."
            ):
                try:
                    analysis = process_intake(user_text)
                    analysis = normalize_analysis(analysis)

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

                    with st.expander("View complete structured JSON"):
                        st.json(analysis)

                except Exception as exc:
                    error_text = str(exc)

                    log_activity(
                        "FAILURE_PATH",
                        f"Error encountered: {error_text}",
                    )

                    st.error(
                        "Execution failed. The application did NOT "
                        "pretend that the work succeeded."
                    )

                    st.code(error_text)

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

                try:
                    params = json.loads(
                        action["parameters"] or "{}"
                    )
                except json.JSONDecodeError:
                    params = {}

                st.markdown("**Proposed parameters:**")
                st.json(params)

                st.markdown("**Current status:**")
                st.info(action["status"])

                col1, col2 = st.columns(2)

                with col1:
                    if st.button(
                        "✅ Approve & Execute",
                        key=f"approve_{action_id}",
                        use_container_width=True,
                    ):
                        ok, result = approve_action(action_id)

                        if ok:
                            st.success(
                                "Action approved and executed."
                            )
                            st.json(json.loads(result))
                            st.rerun()
                        else:
                            st.error(result)

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
                            st.rerun()
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