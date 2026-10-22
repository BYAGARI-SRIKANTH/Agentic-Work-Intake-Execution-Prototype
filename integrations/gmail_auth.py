import os
from pathlib import Path

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

from integrations.gmail_service import GMAIL_SEND_SCOPE


load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def authorize_local_account():
    credentials_file = Path(
        os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    )
    token_file = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))

    if not credentials_file.is_file():
        raise FileNotFoundError(
            "Google OAuth client JSON was not found. Set "
            "GMAIL_CREDENTIALS_FILE to its local path."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_file),
        [GMAIL_SEND_SCOPE],
    )
    credentials = flow.run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    print(f"Gmail authorization saved to {token_file}. Keep this file private.")


if __name__ == "__main__":
    authorize_local_account()
