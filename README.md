# Agentic Work Intake & Execution Prototype

A Python and Streamlit prototype for turning unstructured work requests into validated, reviewable actions. It uses a Groq-hosted language model to interpret requests, applies application-side routing and validation, and records requests and outcomes in SQLite. Actions that contact Gmail or Trello require explicit human approval before execution.

## Project Overview

People often submit work as free-form notes, messages, or instructions. Before that work can be acted on safely, someone must identify the requested task, gather required details, decide which actions are supported, and review any external side effects.

This project demonstrates an AI-assisted intake workflow with a constrained tool set. The model proposes structured actions; Python code validates and routes them. The model does not directly call Gmail, Trello, or the website checker. The application controls execution and records state changes.

## Problem Statement

Unstructured requests can be ambiguous, omit key information, or describe actions that should not happen without review. A prototype that executes model output directly would make it difficult to inspect intent, prevent accidental external actions, and understand what happened after a request.

## Solution

The application converts a request into a structured plan and validates it against supported action types and fields. It can perform bounded local or website-check work, save a local draft, or queue an external action for human review. Approval is tied to the exact stored parameters, and execution state is persisted in SQLite.

## Key Features

- **LLM-assisted request analysis:** Uses Groq chat completions with JSON-object output and configured primary/fallback models.
- **Allowlisted action routing:** Validates model output and action parameters before saving or dispatching work.
- **Human approval for external actions:** Gmail sends and Trello card creation are queued for explicit review. The review shows the proposed details before execution.
- **Multi-recipient email:** Accepts one address or a comma-separated list for the `To` field. Entries are trimmed, validated as a whole, and duplicate addresses are rejected. Cc and Bcc are not implemented.
- **Local-only drafts and task records:** A draft is saved as local output and never sends email. A task record is a prototype result; it does not create a calendar event or external task.
- **Gmail integration:** Sends a plain-text message through the Gmail API after approval, using the local OAuth authorization flow.
- **Trello integration:** Creates a card with title, description, and optional due date after approval.
- **Bounded website checks:** Checks public HTTP(S) sites on standard ports, validates each redirect destination, and pins the connection to a resolved public address.
- **Persistent request history:** Stores original requests, structured interpretations, action parameters, approval/execution states, and external IDs in SQLite.
- **Failure and duplicate-execution safeguards:** Claims an action before calling an external service, sanitizes integration errors, and records uncertain outcomes without automatic retry.
- **Automated tests:** Uses mocked services and test doubles; the suite does not send real email or create live Trello cards.

## Technology Stack

- Python
- Streamlit
- Groq Python SDK and Groq chat completions
- SQLite
- Gmail API, Google OAuth libraries, and `google-auth-httplib2`
- Trello REST API through `requests`
- Python standard-library email, HTTP, networking, and validation modules
- pytest

## How the Application Works

1. A user submits an unstructured request in the Streamlit intake tab.
2. The application sends the request to Groq and asks for a JSON action plan. It uses the configured primary model and fallback model, with limited retries for recognized transient failures.
3. `normalize_analysis` checks the response against supported tools, categories, and parameter keys. It validates email fields and normalizes recipient lists, and validates Trello fields and due dates.
4. The application stores the original request, structured interpretation, and action records in SQLite. Safe automatic actions, such as a bounded website check or a local draft, may run during this routing and save step. A local task record is only produced through its local action handler.
5. Gmail and Trello actions are stored as `PENDING_APPROVAL`. The approval tab displays the stored action details. The approval fingerprint covers the action type and complete parameter object, so a changed proposal no longer matches the reviewed fingerprint.
6. On approval, the executor atomically claims the pending action by marking it `EXECUTING`, then calls the relevant integration. Rejection marks it `REJECTED` without making the external request.
7. The application records the confirmed result or a failure/unknown outcome in SQLite. An uncertain external outcome is not automatically retried; the operator must check the service before taking further action.

In short: **user input → LLM intent/action extraction → validation and routing → local execution or human approval → execution → SQLite status and history**. External sends are never triggered solely by model output.

## Architecture and Workflow

`app.py` owns the Streamlit interface, Groq request, SQLite schema and queries, and routing between the UI and integrations. `integrations/intake_routing.py` validates and normalizes model output. `integrations/action_executor.py` enforces approval checks, fingerprints reviewed parameters, claims actions, and records integration outcomes. Gmail, Trello, and public website behavior is isolated in integration modules.

The UI has three tabs: **Intake & Processing**, **Pending Human Approvals**, and **Database History**. The sidebar displays model configuration and recent activity events.

Action states used by the application include:

- `PENDING_APPROVAL`: waiting for a human decision; the external action has not been submitted.
- `EXECUTING`: claimed by one approval operation while the handler runs.
- `COMPLETED`: local work completed or an external service returned a confirmation.
- `NEEDS_CLARIFICATION`: required data is absent or invalid.
- `REJECTED`: declined by a human and not executed.
- `FAILED`: the integration reported a definitive failure.
- `OUTCOME_UNKNOWN`: the request may have succeeded, but confirmation was not received. The application does not automatically retry it.
- `FLAGGED`: no supported action was selected or the action could not be run as routed.

A Gmail message ID confirms Gmail accepted the submission; it does not confirm delivery to or reading by recipients.

## Project Structure

```text
.
├── app.py
├── integrations/
│   ├── action_executor.py
│   ├── errors.py
│   ├── gmail_auth.py
│   ├── gmail_service.py
│   ├── intake_routing.py
│   ├── safe_web.py
│   └── trello_service.py
├── tests/
│   ├── test_action_executor.py
│   ├── test_gmail_service.py
│   ├── test_intake_routing.py
│   ├── test_safe_web.py
│   └── test_trello_service.py
├── .env.example
├── .gitignore
├── README.md
├── requirements-dev.txt
└── requirements.txt
```

Local-only configuration, OAuth files, databases, and virtual environments are intentionally not part of the project structure shown above.

## Prerequisites

- Python and pip, using a Python version supported by the dependencies in `requirements.txt`.
- Windows PowerShell for the commands below.
- A Groq API key to submit requests to the LLM.
- For local Gmail sends: a Google Cloud project with Gmail API enabled and a Desktop OAuth client.
- For hosted Gmail sends: a Google Cloud **Web application** OAuth client and an authorized refresh token.
- For Trello card creation: a Trello API key, an authorized token, and a target list ID.

Gmail, Trello, and website-check configuration is only needed for the integrations you intend to exercise. The automated tests use mocks and do not need live service credentials.

## Local Installation

Run these commands from the project root in PowerShell:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
```

The execution-policy command only applies to the current PowerShell process. If the virtual environment is already active, skip its creation and activation. Edit `.env` locally to configure the values needed for your integrations; never put real values in source files or this README.

## Environment Variable Configuration

The application loads `.env` from the project root. The app can also read its Groq and integration settings from Streamlit secrets or environment variables. The local Gmail authorization command reads its configuration from the process environment, including values loaded from `.env`.

| Variable | Purpose |
| --- | --- |
| `GROQ_API_KEY` | Required to submit intake requests to Groq. |
| `GMAIL_CREDENTIALS_FILE` | Path to the local Google OAuth client JSON; defaults to `credentials.json`. Used by the local Gmail authorization command. |
| `GMAIL_TOKEN_FILE` | Path where Gmail OAuth authorization is saved and loaded; defaults to `token.json`. |
| `GMAIL_OAUTH_CLIENT_ID` | Web application OAuth client ID for hosted Gmail sending. |
| `GMAIL_OAUTH_CLIENT_SECRET` | Secret for the hosted Web application OAuth client. |
| `GMAIL_REFRESH_TOKEN` | Gmail `gmail.send` refresh token for the account authorized to send. |
| `TRELLO_API_KEY` | Trello API key for card creation. |
| `TRELLO_TOKEN` | Trello account token for card creation. |
| `TRELLO_LIST_ID` | Trello list that receives created cards. |

`.env.example` contains variable names and non-secret path defaults only. Keep actual keys and tokens in local `.env` or an appropriate secret manager, not in Git.

## Streamlit Community Cloud Deployment

The Streamlit entry point is `app.py`, and the root `requirements.txt` declares
the runtime dependencies for the app and its integrations.

1. Push the project to a GitHub repository you control.
2. In [Streamlit Community Cloud](https://share.streamlit.io/), sign in with
   GitHub, choose **Create app**, select this repository and branch, and set
   the main file path to `app.py`.
3. In the app's **Settings → Secrets**, configure the required value using
   Streamlit's TOML format:

   ```toml
   GROQ_API_KEY = "your Groq API key"
   ```

   For Trello card creation, add `TRELLO_API_KEY`, `TRELLO_TOKEN`, and
   `TRELLO_LIST_ID` as additional quoted values in the same secrets file. Do
   not commit or share the secrets file.
4. Deploy and verify the app loads before submitting any requests.

The app currently has no sign-in or user/role authorization. Deploy it only
with access restricted to trusted users; do not make the approval interface
public. Community Cloud's local SQLite file is not durable storage, so request
history may be lost when the app restarts or is redeployed.

For Gmail sending on Streamlit Cloud, add these values to **Settings →
Secrets** (alongside any Groq/Trello values). Do not commit them or put them
in source code:

```toml
GMAIL_OAUTH_CLIENT_ID = "your-web-oauth-client-id"
GMAIL_OAUTH_CLIENT_SECRET = "your-web-oauth-client-secret"
GMAIL_REFRESH_TOKEN = "refresh-token-for-the-authorized-gmail-account"
```

The hosted path uses Google's OAuth 2.0 refresh-token flow with a **Web
application** client. The refresh token is exchanged server-side for a
short-lived access token and is never displayed or written to the app
database. If the hosted OAuth settings are partially configured, Gmail
sending fails with a configuration message. When none are configured, the
app retains the local token-file flow for local development; Community Cloud
normally has no such local token file and displays the hosted setup guidance.
Do not upload OAuth client JSON or `token.json` to the repository.

## Gmail OAuth Setup

For local development, the project continues to use the installed-app OAuth
flow and a private local token file. For Streamlit Cloud, use the separate
Web application OAuth setup described above; a Desktop client and
`token.json` are not a hosted configuration.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and enable the Gmail API.
2. Configure the Google Auth Platform consent screen. If the app is in testing mode, add the account you will authorize as a test user.
3. Create an OAuth client ID with application type **Desktop app** for local
   development.
4. Download the OAuth client JSON to a private local path. By default, the authorization command looks for `credentials.json` in the project root. Alternatively, set `GMAIL_CREDENTIALS_FILE` to the local file path in `.env`.
5. With the project environment active and `.env` configured, run this from the project root:

   ```powershell
   python -m integrations.gmail_auth
   ```

6. Complete Google's consent flow in the local browser. The command saves the authorization to `token.json` by default, or to the path in `GMAIL_TOKEN_FILE`.
7. Start the app, submit an email request, then open **Pending Human Approvals**. Review every recipient, the subject, and the full body before selecting **Approve & Execute**.

The local application requests only the `gmail.send` scope. Keep both OAuth
JSON files private. If authorization expires or is revoked, re-run the local
authorization command. Do not deploy the desktop OAuth client or local token
file to a public server.

### Streamlit Cloud Gmail Authorization

1. In Google Cloud Console, enable the Gmail API and configure the OAuth
   consent screen. Add the sending account as a test user if the app is in
   testing mode.
2. Create a separate OAuth client with type **Web application**. Add this
   exact authorized redirect URI:
   `https://developers.google.com/oauthplayground`
3. Open [Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground).
   Open its settings, enable **Use your own OAuth credentials**, and enter the
   Web client ID and secret. Request only
   `https://www.googleapis.com/auth/gmail.send`, authorize the intended Gmail
   account, and exchange the authorization code for tokens.
4. Copy the returned refresh token directly into the private Streamlit app
   **Settings → Secrets** as `GMAIL_REFRESH_TOKEN`, together with
   `GMAIL_OAUTH_CLIENT_ID` and `GMAIL_OAUTH_CLIENT_SECRET`. Never paste these
   values into chat, commit them, or add them to app code. Close the
   Playground after setup.
5. Restart/redeploy the Streamlit app, then authorize/test by submitting an
   email request and explicitly approving it in **Pending Human Approvals**.

If the OAuth consent screen remains in **Testing**, Google refresh tokens for
Gmail scopes can expire after seven days; publish/verify the consent
configuration as appropriate for your use or repeat authorization when it
expires. Google may require verification for the Gmail scope before broad
external use. If access is revoked or the refresh token expires, create a new
refresh token and replace the Streamlit secret. The app reports an
authentication error without revealing Google response details.

## How to Run the Application

From the project root with the virtual environment active and `GROQ_API_KEY` configured:

```powershell
streamlit run app.py
```

The app creates or migrates `agent_system.db` in the current working directory. This database contains original request text, proposed action parameters, history, and integration results.

## How to Run Automated Tests

From the project root with the development dependencies installed:

```powershell
python -m pytest -q
```

The tests use mocks/test doubles for external services. They do not send real emails or create live Trello cards. Integration behavior against live accounts is not certified by the automated test suite.

## Security Considerations

- Never commit `.env`, OAuth client JSON, token JSON, Streamlit secrets, private keys, local databases, or Python environments. `.gitignore` excludes those files and generated caches; `.env.example` contains blank secret values and path defaults only.
- The previous local repository history contains a committed, revoked Groq API key. Treat that history as compromised and do not publish or push it. The published repository is based on its separate clean history; rotate any other credentials that may have appeared in local history.
- SQLite stores user-submitted request text and proposed email/card content as well as status and external identifiers. Protect the database and any backups as sensitive data.
- The application has no user login, role system, or multi-user authorization boundary. Keep the approval UI on a trusted, access-controlled machine or network; do not expose it as an unauthenticated public application.
- OAuth refresh and token persistence are implemented for local development. Secure production OAuth lifecycle management and durable secret storage are not implemented.
- The website checker allows only HTTP/HTTPS on ports 80 and 443, rejects query strings and fragments, blocks local/private/reserved destinations, and validates redirects. It is not an intranet checker.
- Gmail and Trello integration errors are sanitized before being shown to the user. An unknown send/create outcome is recorded and is not automatically retried. Check the external service before manually attempting the action again.

## Current Limitations

- This is a prototype, not a general-purpose autonomous agent. It supports only the tools and parameter fields defined in the application.
- Model output can be incomplete or misinterpret a request. Validation and approval reduce risk but do not guarantee the model's interpretation is correct.
- Email supports plain-text content and a comma-separated `To` list. Cc, Bcc, attachments, HTML email, and address-book lookup are not implemented.
- Drafts and task records are local prototype results; there is no external calendar or task-management integration for those actions.
- The Gmail OAuth helper is for local development. Production web OAuth, multi-user account management, and hosted token storage are not implemented.
- The Streamlit interface has no authentication or role separation. Human approval is an application workflow, not an identity-verified signature.
- Website checks are intentionally limited to public sites and standard HTTP(S) ports.
- Automated tests do not verify live Groq, Gmail, or Trello accounts, delivery, or external service availability.

## Future Improvements

- Add authenticated users and role-based approval permissions before supporting shared or hosted deployments.
- Design and implement a secure server-side OAuth flow and managed secret/token storage for production use.
- Add structured audit exports, retention controls, and database backup/recovery guidance.
- Expand email capabilities only with explicit review requirements and tests for each new recipient/header or attachment behavior.
- Add stronger observability and integration contract tests while keeping routine tests isolated from live services.
- Add deployment automation and environment-specific configuration after defining a production security model.

## Author and Project Links

- Project repository: https://github.com/BYAGARI-SRIKANTH/Agentic-Work-Intake-Execution-Prototype
- Live demo: not deployed.
