"""
Component A: Universal Ingestion Bus
=====================================

An adapter-based ingestion layer that normalizes status events from multiple
sources (RSS feeds, Slack Socket Mode, incoming webhooks) into a standard
JSON payload and POSTs them to the Logging Server (Component B).

Includes a service onboarding API for registering / removing providers at
runtime.  Provider configs persist in providers.json.

Usage:
    python ingestion_bus.py

Env vars (loaded from .env if present):
    LOG_SERVER_URL  -- Component B base URL (default: http://localhost:8000)
    API_KEY         -- shared secret sent as X-API-KEY (required)
    BUS_PORT        -- port for onboarding API + webhook receiver (default: 8001)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import aiohttp
import feedparser
from aiohttp import web

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_SERVER_URL = os.environ.get("LOG_SERVER_URL", "http://localhost:8000")
API_KEY = os.environ["API_KEY"]
BUS_PORT = int(os.environ.get("BUS_PORT", "8001"))
PROVIDERS_FILE = Path(__file__).parent / "providers.json"

logger = logging.getLogger("ingestion_bus")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# Provider persistence
# ---------------------------------------------------------------------------


def _load_providers() -> list[dict[str, Any]]:
    """Load provider configs from providers.json."""
    if PROVIDERS_FILE.exists():
        try:
            data = json.loads(PROVIDERS_FILE.read_text())
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", PROVIDERS_FILE, exc)
    return []


def _save_providers(providers: list[dict[str, Any]]) -> None:
    """Persist provider configs to providers.json."""
    PROVIDERS_FILE.write_text(json.dumps(providers, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Severity mapping helpers
# ---------------------------------------------------------------------------

_MAJOR_KEYWORDS = re.compile(
    r"degraded|outage|down|error|major|critical|elevated|disruption|impacted|issue|fail",
    re.IGNORECASE,
)
_RESOLVED_KEYWORDS = re.compile(
    r"resolved|restored|recovered|fixed|completed|operational",
    re.IGNORECASE,
)


def _infer_severity(text: str) -> str:
    """Infer severity from free-text status content."""
    if _RESOLVED_KEYWORDS.search(text):
        return "resolved"
    if _MAJOR_KEYWORDS.search(text):
        return "major"
    return "info"


# ---------------------------------------------------------------------------
# Base adapter
# ---------------------------------------------------------------------------


class BaseAdapter(ABC):
    """
    Abstract adapter.  Subclasses implement start/stop and call self.emit()
    to forward normalized events to Component B.
    """

    def __init__(
        self,
        provider_name: str,
        session: aiohttp.ClientSession,
    ):
        self.provider_name = provider_name
        self._session = session

    @abstractmethod
    async def start(self) -> None:
        """Begin consuming events.  Must be a long-running coroutine."""

    @abstractmethod
    async def stop(self) -> None:
        """Clean up resources."""

    async def emit(
        self,
        status: str,
        message: str,
        severity: str,
        timestamp: str | None = None,
    ) -> None:
        """POST a normalized StatusEvent to the Logging Server."""
        payload = {
            "provider": self.provider_name,
            "status": status,
            "message": message,
            "severity": severity,
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        }
        try:
            async with self._session.post(
                f"{LOG_SERVER_URL}/log",
                json=payload,
                headers={"X-API-KEY": API_KEY},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(
                        "Logging server returned %d for %s: %s",
                        resp.status,
                        self.provider_name,
                        body,
                    )
        except aiohttp.ClientError as exc:
            logger.error(
                "Failed to emit event for %s: %s", self.provider_name, exc
            )


# ---------------------------------------------------------------------------
# RSS Smart-Poller Adapter
# ---------------------------------------------------------------------------


class RSSPollerAdapter(BaseAdapter):
    """
    Polls an Atom/RSS feed using conditional HTTP requests
    (ETag / If-Modified-Since) for near-zero bandwidth at idle.
    Falls back to entry-ID dedup if the server ignores conditional headers.
    """

    def __init__(
        self,
        provider_name: str,
        session: aiohttp.ClientSession,
        feed_url: str,
        poll_interval: int = 60,
    ):
        super().__init__(provider_name, session)
        self.feed_url = feed_url
        self.poll_interval = poll_interval

        self._etag: str | None = None
        self._last_modified: str | None = None
        self._seen_ids: set[str] = set()
        self._running = False

    async def start(self) -> None:
        self._running = True
        first_run = True
        logger.info(
            "RSS adapter started for %s (feed: %s, interval: %ds)",
            self.provider_name,
            self.feed_url,
            self.poll_interval,
        )
        while self._running:
            try:
                await self._poll(first_run)
                first_run = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "RSS poll error for %s: %s", self.provider_name, exc
                )
            await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self._running = False

    async def _poll(self, first_run: bool) -> None:
        headers: dict[str, str] = {"Accept-Encoding": "gzip, deflate"}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        async with self._session.get(self.feed_url, headers=headers) as resp:
            if resp.status == 304:
                logger.debug(
                    "No changes for %s (304 Not Modified)", self.provider_name
                )
                return

            if resp.status != 200:
                logger.warning(
                    "Feed %s returned HTTP %d", self.feed_url, resp.status
                )
                return

            # Cache conditional-request tokens
            if "ETag" in resp.headers:
                self._etag = resp.headers["ETag"]
            if "Last-Modified" in resp.headers:
                self._last_modified = resp.headers["Last-Modified"]

            body = await resp.text()

        feed = feedparser.parse(body)
        new_entries = []
        for entry in feed.entries:
            entry_id = entry.get("id", entry.get("link", ""))
            if entry_id and entry_id not in self._seen_ids:
                self._seen_ids.add(entry_id)
                new_entries.append(entry)

        if first_run:
            logger.info(
                "Loaded %d feed entries for %s (%d new)",
                len(feed.entries),
                self.provider_name,
                len(new_entries),
            )

        # On first run, emit only the most recent 5 to avoid flooding
        entries_to_emit = new_entries[:5] if first_run else new_entries

        for entry in entries_to_emit:
            title = entry.get("title", "Status update")
            summary = entry.get("summary", entry.get("description", ""))
            # Strip HTML tags from summary
            summary_clean = re.sub(r"<[^>]+>", "", summary).strip()
            published = entry.get("published", entry.get("updated", ""))
            severity = _infer_severity(f"{title} {summary_clean}")
            await self.emit(
                status=title,
                message=summary_clean[:500] if summary_clean else "No details",
                severity=severity,
                timestamp=published or None,
            )


# ---------------------------------------------------------------------------
# Slack Socket Mode Adapter
# ---------------------------------------------------------------------------


class SlackAdapter(BaseAdapter):
    """
    Listens for status messages via Slack Socket Mode (WebSocket-based,
    no public URL needed).  Requires a Slack app with Socket Mode enabled.

    Uses the **synchronous** SocketModeClient running in a daemon thread.
    The async variant has a known event-loop mismatch bug on Python 3.10+
    (internal asyncio.Queue binds to a stale loop).  The sync handler
    avoids this entirely.
    """

    def __init__(
        self,
        provider_name: str,
        session: aiohttp.ClientSession,
        app_token: str,
        bot_token: str,
        channel_id: str,
    ):
        super().__init__(provider_name, session)
        self.app_token = app_token
        self.bot_token = bot_token
        self.channel_id = channel_id
        self._thread = None

    async def start(self) -> None:
        try:
            from slack_sdk.web import WebClient
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.request import SocketModeRequest
            from slack_sdk.socket_mode.response import SocketModeResponse
        except ImportError:
            logger.error(
                "slack_sdk is required for the Slack adapter. "
                "Install it with: pip install slack_sdk"
            )
            return

        import threading

        loop = asyncio.get_running_loop()
        adapter_ref = self

        web_client = WebClient(token=self.bot_token)
        client = SocketModeClient(
            app_token=self.app_token,
            web_client=web_client,
        )

        def _on_message(
            sock_client: SocketModeClient, req: SocketModeRequest
        ) -> None:
            sock_client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
            if req.type != "events_api":
                return

            event = req.payload.get("event", {})
            if event.get("type") != "message":
                return
            if event.get("channel") != adapter_ref.channel_id:
                return

            text = event.get("text", "")
            if not text:
                return

            severity = _infer_severity(text)
            lines = text.strip().split("\n", 1)
            status_text = lines[0][:200]
            msg_text = lines[1][:500] if len(lines) > 1 else status_text

            # Schedule async emit back on the main event loop
            asyncio.run_coroutine_threadsafe(
                adapter_ref.emit(
                    status=status_text,
                    message=msg_text,
                    severity=severity,
                ),
                loop,
            )

        client.socket_mode_request_listeners.append(_on_message)

        def _run() -> None:
            import time
            try:
                client.connect()
                while True:
                    time.sleep(1)
            except Exception as exc:
                logger.error(
                    "Slack thread error for %s: %s",
                    adapter_ref.provider_name,
                    exc,
                )

        self._thread = threading.Thread(
            target=_run,
            name=f"slack-{self.provider_name}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Slack adapter started for %s (channel: %s)",
            self.provider_name,
            self.channel_id,
        )

        # Keep the async task alive
        while True:
            await asyncio.sleep(60)

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Webhook Receiver Adapter
# ---------------------------------------------------------------------------


class WebhookReceiverAdapter(BaseAdapter):
    """
    Receives incoming webhooks (from email bridges, Zapier, custom
    integrations, etc.) via a catch-all route on the bus's HTTP server.

    The adapter normalizes incoming POST payloads and forwards them to
    Component B.  It expects JSON with at least a 'text' or 'message' field.

    NOTE: Routes cannot be added to aiohttp after startup (the router
    freezes).  Instead, a single catch-all route /hook/{name} is
    registered at startup, and this adapter is looked up by name.
    """

    def __init__(
        self,
        provider_name: str,
        session: aiohttp.ClientSession,
        path_prefix: str,
    ):
        super().__init__(provider_name, session)
        self.path_prefix = path_prefix.rstrip("/")

    async def start(self) -> None:
        logger.info(
            "Webhook adapter active for %s at /hook/%s",
            self.provider_name,
            self.provider_name,
        )
        # Keep alive
        while True:
            await asyncio.sleep(3600)

    async def stop(self) -> None:
        pass

    async def handle_webhook(self, data: dict[str, Any]) -> None:
        """Process an incoming webhook payload."""
        # Try to extract meaningful fields from various payload shapes
        text = (
            data.get("message")
            or data.get("text")
            or data.get("body")
            or json.dumps(data)
        )
        status_val = data.get("status", data.get("subject", str(text)[:200]))
        severity = data.get("severity", _infer_severity(str(text)))

        await self.emit(
            status=str(status_val)[:200],
            message=str(text)[:500],
            severity=severity,
        )


# ---------------------------------------------------------------------------
# Adapter Registry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """
    Manages running adapters.  Providers can be added or removed at runtime.
    Each adapter runs as an asyncio task.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        webhook_app: web.Application,
    ):
        self._session = session
        self._webhook_app = webhook_app
        self._adapters: dict[str, BaseAdapter] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._providers: list[dict[str, Any]] = []

    @property
    def providers(self) -> list[dict[str, Any]]:
        return list(self._providers)

    def _create_adapter(self, provider: dict[str, Any]) -> BaseAdapter:
        """Factory: create an adapter from a provider config dict."""
        name = provider["name"]
        source = provider["source"]
        config = provider.get("config", {})

        if source == "rss":
            return RSSPollerAdapter(
                provider_name=name,
                session=self._session,
                feed_url=config["feed_url"],
                poll_interval=config.get("poll_interval", 60),
            )
        elif source == "slack":
            return SlackAdapter(
                provider_name=name,
                session=self._session,
                app_token=config["app_token"],
                bot_token=config["bot_token"],
                channel_id=config["channel_id"],
            )
        elif source == "webhook":
            return WebhookReceiverAdapter(
                provider_name=name,
                session=self._session,
                path_prefix=config.get("path_prefix", f"/hook/{name.lower()}"),
            )
        else:
            raise ValueError(f"Unknown source type: {source}")

    async def add_provider(self, provider: dict[str, Any]) -> None:
        """Onboard a provider: create adapter, persist config, start task."""
        name = provider["name"]
        if name in self._adapters:
            raise ValueError(f"Provider '{name}' is already registered")

        adapter = self._create_adapter(provider)
        self._adapters[name] = adapter
        self._providers.append(provider)
        _save_providers(self._providers)

        task = asyncio.create_task(adapter.start(), name=f"adapter-{name}")
        self._tasks[name] = task
        logger.info("Provider onboarded: %s (source: %s)", name, provider["source"])

    async def remove_provider(self, name: str) -> None:
        """Remove a provider: cancel task, clean up, persist."""
        if name not in self._adapters:
            raise KeyError(f"Provider '{name}' not found")

        adapter = self._adapters.pop(name)
        await adapter.stop()

        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._providers = [p for p in self._providers if p["name"] != name]
        _save_providers(self._providers)
        logger.info("Provider removed: %s", name)

    async def load_from_file(self) -> None:
        """Load providers from providers.json and start their adapters."""
        providers = _load_providers()
        for provider in providers:
            try:
                await self.add_provider(provider)
            except Exception as exc:
                logger.error(
                    "Failed to load provider %s: %s",
                    provider.get("name", "?"),
                    exc,
                )


# ---------------------------------------------------------------------------
# Onboarding API (aiohttp routes)
# ---------------------------------------------------------------------------

_registry: AdapterRegistry | None = None


async def _handle_list_providers(request: web.Request) -> web.Response:
    """GET /providers -- list all registered providers."""
    return web.json_response(_registry.providers)


async def _handle_add_provider(request: web.Request) -> web.Response:
    """POST /providers -- onboard a new provider."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Basic validation
    name = data.get("name")
    source = data.get("source")
    config = data.get("config", {})

    if not name or not source:
        return web.json_response(
            {"error": "Fields 'name' and 'source' are required"}, status=400
        )

    if source not in ("rss", "slack", "webhook"):
        return web.json_response(
            {"error": f"Unknown source type: {source}. Must be rss, slack, or webhook"},
            status=400,
        )

    # Source-specific config validation
    if source == "rss" and "feed_url" not in config:
        return web.json_response(
            {"error": "RSS source requires 'feed_url' in config"}, status=400
        )
    if source == "slack":
        missing = [
            k for k in ("app_token", "bot_token", "channel_id") if k not in config
        ]
        if missing:
            return web.json_response(
                {"error": f"Slack source requires {missing} in config"}, status=400
            )

    try:
        await _registry.add_provider(data)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)

    return web.json_response({"status": "onboarded", "provider": name}, status=201)


async def _handle_delete_provider(request: web.Request) -> web.Response:
    """DELETE /providers/{name} -- remove a provider."""
    name = request.match_info["name"]
    try:
        await _registry.remove_provider(name)
    except KeyError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    return web.json_response({"status": "removed", "provider": name})


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_bus() -> None:
    """Start the ingestion bus: load providers, start onboarding API."""
    global _registry

    connector = aiohttp.TCPConnector(limit_per_host=5)
    timeout = aiohttp.ClientTimeout(total=30)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    app = web.Application()
    _registry = AdapterRegistry(session=session, webhook_app=app)

    # Onboarding API routes
    app.router.add_get("/providers", _handle_list_providers)
    app.router.add_post("/providers", _handle_add_provider)
    app.router.add_delete("/providers/{name}", _handle_delete_provider)

    # Catch-all webhook route -- must be registered at startup because
    # aiohttp freezes the router after the app starts.
    async def _handle_webhook(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        adapter = _registry._adapters.get(name)
        if not adapter or not isinstance(adapter, WebhookReceiverAdapter):
            return web.json_response(
                {"error": f"No webhook adapter for '{name}'"}, status=404
            )
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        await adapter.handle_webhook(data)
        return web.json_response({"status": "received"})

    app.router.add_post("/hook/{name}", _handle_webhook)

    # Health check
    app.router.add_get("/health", lambda r: web.json_response({"status": "ok"}))

    # Load saved providers
    await _registry.load_from_file()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", BUS_PORT)
    await site.start()

    logger.info(
        "Ingestion bus started on port %d -- "
        "POST /providers to onboard, GET /providers to list",
        BUS_PORT,
    )

    try:
        # Run forever
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await session.close()
        await runner.cleanup()


async def _main_async() -> None:
    """Async main: set up signal handling and run the bus."""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # Start the bus in a task
    bus_task = asyncio.create_task(run_bus())

    # Wait for a signal
    await stop_event.wait()

    # Shut down gracefully
    bus_task.cancel()
    try:
        await bus_task
    except asyncio.CancelledError:
        pass
    print("\nIngestion bus stopped.")


def main() -> None:
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
