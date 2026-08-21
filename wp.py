import os
import requests
from dotenv import load_dotenv

load_dotenv()

WIREBASE_API_URL = "https://wirebase.sanjivanitechno.com/api/public/send"
WIREBASE_API_KEY = os.getenv("WIREBASE_API_KEY")
WIREBASE_INSTANCE_NAME = os.getenv("WIREBASE_INSTANCE_NAME", "Nishahomes")


class WirebaseError(Exception):
    """Raised for any Wirebase send failure (network, auth, disconnected instance, etc.)."""
    pass


def send_whatsapp_message(to, message, instance_name=None, msg_type="text", timeout=30):
    """
    Sends a WhatsApp text message via Wirebase.

    Args:
        to: recipient number, digits only with country code (e.g. "919876543210").
        message: message body.
        instance_name: overrides WIREBASE_INSTANCE_NAME for this call if given.
        msg_type: Wirebase message type, defaults to "text".
        timeout: request timeout in seconds.

    Returns:
        dict — the parsed JSON success payload, e.g.
        {"success": true, "instanceId": "...", "instanceName": "...", "messageId": "..."}

    Raises:
        WirebaseError on any failure (missing config, network error, non-2xx response,
        or a JSON body without "success": true).
    """
    if not WIREBASE_API_KEY:
        raise WirebaseError("WIREBASE_API_KEY not configured in .env")

    to = str(to or "").strip()
    if not to:
        raise WirebaseError("Recipient number is required")

    message = (message or "").strip()
    if not message:
        raise WirebaseError("Message body is required")

    payload = {
        "instanceName": instance_name or WIREBASE_INSTANCE_NAME,
        "to": to,
        "type": msg_type,
        "message": message,
    }

    try:
        resp = requests.post(
            WIREBASE_API_URL,
            headers={"X-API-Key": WIREBASE_API_KEY},
            json=payload,
            timeout=timeout,
        )
    except requests.exceptions.RequestException as e:
        raise WirebaseError(f"Network error contacting Wirebase: {e}")

    try:
        data = resp.json()
    except ValueError:
        data = {}

    if resp.status_code >= 400 or not data.get("success"):
        err_msg = data.get("error") or f"Wirebase send failed (HTTP {resp.status_code})"
        raise WirebaseError(err_msg)

    return data