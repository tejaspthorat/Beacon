"""
Sample script: Onboard providers for each ingestion type.

Prerequisites:
  - Component B (logging_server.py) running on port 8000
  - Component A (ingestion_bus.py)  running on port 8001
"""

import requests

BUS_URL = "http://localhost:8001"


def onboard_rss_provider():
    """
    RSS Smart-Poller -- fallback for providers without a push channel.
    The adapter polls the Atom/RSS feed with conditional HTTP requests
    (ETag / If-Modified-Since) so it is near-zero bandwidth at idle.
    """
    resp = requests.post(
        f"{BUS_URL}/providers",
        json={
            "name": "OpenAI",
            "source": "rss",
            "config": {
                "feed_url": "https://status.openai.com/history.atom",
                "poll_interval": 60,  # seconds between polls (default: 60)
            },
        },
    )
    print(f"RSS onboard: {resp.status_code} {resp.json()}")


def onboard_slack_provider():
    """
    Slack Socket Mode -- truly event-based, no public URL needed.
    The adapter opens a persistent WebSocket to Slack and listens
    for messages in the configured channel.
    """
    resp = requests.post(
        f"{BUS_URL}/providers",
        json={
            "name": "Openai",
            "source": "slack",
            "config": {
                # App-Level Token (Settings > Basic Information > App-Level Tokens)
                "app_token": "[APP_TOKEN]",
                # Bot User OAuth Token (Settings > OAuth & Permissions)
                "bot_token": "[BOT_TOKEN]",
                # Channel ID where the status bot posts (right-click channel > View details)
                "channel_id": "[CHANNEL_ID]",
            },
        },
    )
    print(f"Slack onboard: {resp.status_code} {resp.json()}")


def onboard_webhook_provider():
    """
    Webhook Receiver -- for email-to-webhook bridges, Zapier, or any
    external tool that can POST JSON.

    After onboarding, external tools POST to:
      http://localhost:8001/hook/pagerduty

    The payload should have at least a 'message' or 'text' field.
    Optional fields: 'status', 'severity' (info|major|resolved).
    """
    resp = requests.post(
        f"{BUS_URL}/providers",
        json={
            "name": "PagerDuty",
            "source": "webhook",
            "config": {
                # The bus will listen for POSTs at this path
                "path_prefix": "/hook/pagerduty",
            },
        },
    )
    print(f"Webhook onboard: {resp.status_code} {resp.json()}")


def list_providers():
    """List all currently registered providers."""
    resp = requests.get(f"{BUS_URL}/providers")
    print(f"Active providers: {resp.json()}")


def remove_provider(name: str):
    """Remove a provider by name."""
    resp = requests.delete(f"{BUS_URL}/providers/{name}")
    print(f"Remove {name}: {resp.status_code} {resp.json()}")


def simulate_webhook_event():
    """
    Simulate an external tool sending a webhook to the PagerDuty adapter.
    This is what Zapier, an email bridge, or a custom script would POST.
    """
    resp = requests.post(
        f"{BUS_URL}/hook/PagerDuty",
        json={
            "status": "Database failover triggered",
            "message": "Primary DB is unreachable. Automated failover to replica initiated.",
            "severity": "major",
        },
    )
    try:
        print(f"Webhook event: {resp.status_code} {resp.json()}")
    except Exception:
        print(f"Webhook event: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    # print("--- Onboarding RSS provider ---")
    # onboard_rss_provider()

    # print("\n--- Onboarding Slack provider ---")
    # onboard_slack_provider()

    # Step 1: Register the webhook endpoint first
    # print("--- Onboarding Webhook provider ---")
    # onboard_webhook_provider()

    # Step 2: Now simulate an event hitting that endpoint
    print("\n--- Simulating a webhook event ---")
    simulate_webhook_event()

    # Uncomment to clean up:
    # print("\n--- Removing providers ---")
    # remove_provider("OpenAI")
    # remove_provider("AWS")
    # remove_provider("PagerDuty")
