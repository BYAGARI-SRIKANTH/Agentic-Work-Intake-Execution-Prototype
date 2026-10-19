# Agentic-Work-Intake-Execution-Prototype

A prototype Streamlit app for turning incoming work requests into reviewable agent workflows.

## Run locally

1. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. Set `GROQ_API_KEY` in your environment or add it to a local `.env` file.
3. Start the app:

   ```powershell
   streamlit run app.py
   ```

The app creates its SQLite database locally. Credentials, database files, and Python caches are excluded from Git.
