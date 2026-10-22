# Agentic Work Intake & Execution Prototype

An AI-powered workflow automation prototype that transforms unstructured work requests into structured, validated actions—with human approval before external actions are executed.

Built with **Python, Streamlit, Groq LLM, SQLite, Gmail API, and Trello API**.

<p align="center">
  <a href="https://agentic-work-intake-execution-prototype-bscbkjzkagduk5bk2mxq4q.streamlit.app/">
    <strong>🚀 Try the Live Demo</strong>
  </a>
  &nbsp; | &nbsp;
  <a href="https://github.com/BYAGARI-SRIKANTH/Agentic-Work-Intake-Execution-Prototype">
    <strong>📂 View Source Code</strong>
  </a>
</p>

---

## 📌 Project Overview

Work requests often arrive as unstructured emails, notes, or natural-language instructions. Turning them into actionable tasks requires understanding the request, extracting important details, validating the information, and deciding what should happen next.

This project demonstrates how an LLM can help automate that workflow while keeping execution under application control.

A user submits a request in natural language. The LLM proposes a structured action plan, Python validates the plan, and the application either performs supported local actions or requests human approval before sending an email or creating a Trello card.

**The core principle: AI proposes actions; application logic validates them; a human approves external actions.**

## 🎯 Problem Statement

Unstructured work requests can be ambiguous, incomplete, or risky to execute automatically.

Executing raw LLM output directly can lead to incorrect actions or unintended external side effects.

This prototype addresses those challenges through structured action extraction, validation, controlled execution, explicit approval, and persistent request history.

## 💡 Key Features

* **Natural-language intake:** Accepts work requests written in plain English.
* **LLM-powered analysis:** Uses the Groq API to interpret requests and generate structured action plans.
* **Action validation:** Checks supported action types, parameters, and required fields before execution.
* **Human approval workflow:** Requires explicit approval before Gmail emails are sent or Trello cards are created.
* **Gmail integration:** Sends plain-text emails through the Gmail API after approval.
* **Trello integration:** Creates Trello cards with supported details after approval.
* **Local draft handling:** Saves email drafts locally without sending them.
* **Safe website checks:** Supports bounded checks of eligible public HTTP/HTTPS websites.
* **SQLite persistence:** Stores requests, action parameters, execution states, and results.
* **Execution safeguards:** Tracks action states and avoids automatically retrying actions with uncertain outcomes.
* **Automated testing:** Includes tests for routing, action execution, Gmail, Trello, and website-check behavior.

## 🛠️ Technology Stack

| Technology       | Purpose                                      |
| ---------------- | -------------------------------------------- |
| Python           | Core application logic                       |
| Streamlit        | Interactive web interface                    |
| Groq API         | LLM-powered request analysis                 |
| SQLite           | Request history and action-state persistence |
| Gmail API        | Approved email sending                       |
| Google OAuth 2.0 | Gmail authorization                          |
| Trello REST API  | Approved card creation                       |
| Requests         | HTTP communication                           |
| Pytest           | Automated testing                            |

## 🔄 How It Works

```text
       User submits a request
                 |
                 v
       Groq LLM analyzes intent
                 |
                 v
       Structured action plan
                 |
                 v
       Python validation
                 |
                 v
       Action routing
          /          \
         /            \
        v              v
 Local action      External action
        |              |
        v              v
 Execute locally   Human reviews details
        |              |
        |         +----+----+
        |         |         |
        |       Approve   Reject
        |         |         |
        |         v         v
        |      Execute   Do not execute
        |         |
        +----+----+
             |
             v
      Save status in SQLite
             |
             v
      Display result/history
```

### Workflow explained

1. **Intake:** The user enters a natural-language request.
2. **Analysis:** The application sends the request to the Groq API for structured interpretation.
3. **Validation:** Python checks the proposed action against the supported action types and parameter rules.
4. **Routing:** The application determines whether the action can run locally or requires approval.
5. **Human review:** Gmail and Trello actions appear in the approval interface with their proposed details.
6. **Execution:** After approval, the application invokes the relevant integration.
7. **Persistence:** SQLite records the request, action state, and available execution result.

External actions are not executed solely because the LLM proposed them.

## 🧱 Project Architecture

The application separates its main responsibilities across the user interface, validation logic, execution controls, and integrations.

| Component                         | Responsibility                                                    |
| --------------------------------- | ----------------------------------------------------------------- |
| `app.py`                          | Streamlit interface, request processing, and application workflow |
| `integrations/intake_routing.py`  | Validates and normalizes structured action proposals              |
| `integrations/action_executor.py` | Enforces approval checks and manages execution states             |
| `integrations/gmail_service.py`   | Gmail API integration                                             |
| `integrations/gmail_auth.py`      | Local Gmail OAuth authorization                                   |
| `integrations/trello_service.py`  | Trello card creation                                              |
| `integrations/safe_web.py`        | Restricted public website checks                                  |
| `integrations/errors.py`          | Integration error handling                                        |
| `tests/`                          | Automated tests for core behavior and integrations                |

The interface includes three main areas:

* **Intake & Processing:** Submit and process requests.
* **Pending Human Approvals:** Review, approve, or reject supported external actions.
* **Database History:** Inspect stored requests and action outcomes.

## 📂 Project Structure

```text
Agentic-Work-Intake-Execution-Prototype/
│
├── app.py
├── integrations/
│   ├── action_executor.py
│   ├── errors.py
│   ├── gmail_auth.py
│   ├── gmail_service.py
│   ├── intake_routing.py
│   ├── safe_web.py
│   └── trello_service.py
│
├── tests/
│   ├── test_action_executor.py
│   ├── test_gmail_service.py
│   ├── test_intake_routing.py
│   ├── test_safe_web.py
│   └── test_trello_service.py
│
├── .env.example
├── .gitignore
├── README.md
├── requirements.txt
└── requirements-dev.txt
```

## 🚀 Run Locally

### Prerequisites

* Python installed on your computer.
* A Groq API key.
* Gmail and Trello credentials if you want to use those integrations.

### 1. Clone the repository

```bash
git clone https://github.com/BYAGARI-SRIKANTH/Agentic-Work-Intake-Execution-Prototype.git
cd Agentic-Work-Intake-Execution-Prototype
```

### 2. Create a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

### 4. Configure environment variables

Copy the example configuration:

```powershell
Copy-Item .env.example .env
```

Open `.env` and configure the values required for the integrations you intend to use.

Example:

```env
GROQ_API_KEY=your_groq_api_key
```

Additional configuration may be required for Gmail and Trello. See the sections below.

**Important:** Never commit your real API keys, OAuth credentials, refresh tokens, or `.env` file to GitHub.

### 5. Start the application

```bash
streamlit run app.py
```

Streamlit will display a local URL in your terminal. Open that URL in your browser to use the application.

## 🔐 Configure Gmail and Trello

### Gmail

To enable email sending:

1. Enable the Gmail API in Google Cloud.
2. Configure OAuth consent and authorize the Gmail account that will send messages.
3. Configure the appropriate OAuth credentials for your environment.
4. Submit an email request in the application.
5. Open the approval section and review the recipient, subject, and message body.
6. Approve the action to execute it.

For local development, the project supports a local OAuth token-file flow. For the hosted application, configure the supported Gmail OAuth client ID, client secret, and refresh token through Streamlit Secrets.

### Trello

To enable Trello card creation, configure:

```env
TRELLO_API_KEY=your_trello_api_key
TRELLO_TOKEN=your_trello_token
TRELLO_LIST_ID=your_trello_list_id
```

Use real credentials only in your private local environment or an appropriate secrets manager.

Trello actions must go through the application's approval workflow before execution.

## ☁️ Deployment

The application can be deployed using Streamlit Community Cloud.

1. Push the project to your GitHub repository.
2. Open [Streamlit Community Cloud](https://share.streamlit.io/).
3. Create an application and select your repository and branch.
4. Set the application entry point to `app.py`.
5. Configure the required secrets in the application's settings.
6. Deploy and test the application.

### Live Demo

Try the deployed application:

**[Open Agentic Work Intake & Execution Prototype](https://agentic-work-intake-execution-prototype-bscbkjzkagduk5bk2mxq4q.streamlit.app/)**

**Security note:** The application does not currently implement user authentication or role-based access control. Restrict access to trusted users. Do not expose the approval interface as an unrestricted public application.

Streamlit Community Cloud's local SQLite storage should not be treated as durable production storage.

## 🧪 Run Tests

Run the automated test suite from the project root:

```bash
python -m pytest -q
```

The tests use mocks or test doubles for external integrations. They are designed to test application behavior without sending real emails or creating live Trello cards.

Automated tests do not guarantee live third-party service availability or successful email delivery.

## 🔒 Security and Reliability

This project demonstrates several practical safeguards:

* Validates model-generated action parameters before routing.
* Requires human approval for Gmail and Trello actions.
* Checks the stored action details against the reviewed approval fingerprint.
* Tracks action execution states in SQLite.
* Restricts website checks to eligible public HTTP/HTTPS destinations.
* Records uncertain external outcomes rather than automatically retrying them.
* Keeps credentials out of source code through environment variables and Streamlit Secrets.

### Current Security Limitations

This is a learning and portfolio prototype, not a production-ready multi-user automation platform.

It does not currently provide user authentication, role-based permissions, durable hosted database storage, or a complete production OAuth lifecycle.

The human approval workflow is an application-level safeguard; it is not an identity-verified approval signature.

## ⚠️ Current Limitations

* The application supports a defined set of actions rather than arbitrary tools.
* LLM output can still misunderstand an ambiguous request.
* Email content is plain text; advanced email features are not implemented.
* Local drafts do not send email.
* Local task records do not automatically create external calendar events.
* Gmail and Trello behavior depends on valid credentials and external service availability.
* SQLite history may not survive hosted application restarts or redeployments.
* The application has not been presented as a production-ready autonomous agent.

## 🔮 Future Improvements

* Add authentication and role-based approval permissions.
* Add durable database storage and backup/recovery support.
* Improve monitoring and structured audit logs.
* Add additional integrations with explicit approval and validation rules.
* Improve deployment automation and configuration management.
* Add further integration testing and reliability monitoring.

## 👨‍💻 Author

**Srikanth Byagari**

Computer Science Engineering graduate interested in Python development, AI engineering, LLM applications, and workflow automation.

### Project Links

* **GitHub Repository:** https://github.com/BYAGARI-SRIKANTH/Agentic-Work-Intake-Execution-Prototype
* **Live Demo:** https://agentic-work-intake-execution-prototype-bscbkjzkagduk5bk2mxq4q.streamlit.app/

---

*Built as a hands-on project to explore LLM integration, Python application architecture, API integrations, approval workflows, and persistent action tracking.*
