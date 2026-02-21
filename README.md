# Service Status Tracking System

A decoupled, event-driven system for tracking service status updates from 100+ providers (OpenAI, AWS, GitHub, Stripe, etc.) in real time.

---

## Architecture

```
                     COMPONENT A                                  COMPONENT B
            Universal Ingestion Bus (:8001)                  Logging Server (:8000)
+--------------------------------------------------+    +-------------------------+
|                                                  |    |                         |
|  +-----------------+                             |    |     POST /log           |
|  | Slack Adapter   |--- Socket Mode (WebSocket)  |    |     (X-API-KEY auth)    |
|  | (event-based)   |                             |    |                         |
|  +-----------------+     +--------------+        |    |   Color-coded console:  |
|                     +--->| Normalizer   |--POST--+--->|   RED   = major/outage  |
|  +-----------------+     | (standard    |        |    |   YELLOW = info         |
|  | Webhook Adapter |---->|  JSON schema)|        |    |   GREEN  = resolved     |
|  | (event-based)   |     +--------------+        |    |                         |
|  +-----------------+                             |    +-------------------------+
|                                                  |
|  +-----------------+                             |
|  | RSS Poller      |--- ETag / If-Modified-Since |
|  | (smart fallback)|                             |
|  +-----------------+                             |
|                                                  |
|  Onboarding API:                                 |
|    POST   /providers  -- register a provider     |
|    GET    /providers  -- list all providers       |
|    DELETE /providers/{name} -- remove a provider  |
+--------------------------------------------------+
```

**Component A** ingests events from heterogeneous sources, normalizes them into a standard JSON schema, and POSTs to **Component B**.

**Component B** is a pure receiver -- it never polls anything. It validates an API key, then prints each event to the console with ANSI color coding.

---

## Standard JSON Schema

Every event flowing between the two components uses this contract:

```json
{
  "provider":  "OpenAI",
  "status":    "Chat Completions -- Degraded Performance",
  "message":   "We are investigating elevated error rates.",
  "severity":  "major",
  "timestamp": "2025-11-03T14:32:00Z"
}
```

| Field      | Type   | Values                                |
|------------|--------|---------------------------------------|
| provider   | string | Name of the service provider          |
| status     | string | Affected service and current state    |
| message    | string | Human-readable description            |
| severity   | enum   | `"info"` \| `"major"` \| `"resolved"` |
| timestamp  | string | ISO 8601 timestamp                    |

---

## Quick Start

### 1. Install dependencies

```bash
cd /path/to/bolna-task
pip install -r requirements.txt
```

### 2. Start Component B (Logging Server)

```bash
python logging_server.py
```

Starts on port **8000**. Override with `PORT=9000 python logging_server.py`.

### 3. Start Component A (Ingestion Bus)

```bash
python ingestion_bus.py
```

Starts on port **8001**. Override with `BUS_PORT=9001 python ingestion_bus.py`.

On startup, the bus loads `providers.json` and starts adapters for each registered provider automatically.

### 4. Test with curl

```bash
curl -X POST http://localhost:8000/log \
  -H "Content-Type: application/json" \
  -H "X-API-KEY: dev-secret-key" \
  -d '{
    "provider": "OpenAI",
    "status": "Chat Completions -- Major Outage",
    "message": "Investigating elevated error rates",
    "severity": "major",
    "timestamp": "2025-11-03T14:32:00Z"
  }'
```

You should see a **RED** `[MAJOR]` line appear in the logging server console.

---

## Onboarding Providers

Providers are registered at runtime via the Ingestion Bus API. Each requires a `name`, `source` type, and source-specific `config`.

### RSS Provider (Smart Polling Fallback)

For any provider with an Atom/RSS status feed:

```bash
curl -X POST http://localhost:8001/providers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenAI",
    "source": "rss",
    "config": {
      "feed_url": "https://status.openai.com/history.atom",
      "poll_interval": 60
    }
  }'
```

| Config Field    | Required | Default | Description                  |
|-----------------|----------|---------|------------------------------|
| `feed_url`      | Yes      | --      | Atom/RSS feed URL            |
| `poll_interval` | No       | 60      | Seconds between poll cycles  |

Common status feed URLs:

| Provider | Feed URL                                       |
|----------|------------------------------------------------|
| OpenAI   | `https://status.openai.com/history.atom`       |
| GitHub   | `https://www.githubstatus.com/history.atom`    |
| Stripe   | `https://status.stripe.com/history.atom`       |
| Twilio   | `https://status.twilio.com/history.atom`       |

### Slack Provider (Event-Based)

For providers that offer a Slack integration (most Statuspage.io pages do):

```bash
curl -X POST http://localhost:8001/providers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "AWS",
    "source": "slack",
    "config": {
      "app_token": "xapp-1-A0123...",
      "bot_token": "xoxb-0123...",
      "channel_id": "C0123ABCDEF"
    }
  }'
```

| Config Field  | Required | Where to Find                                           |
|---------------|----------|---------------------------------------------------------|
| `app_token`   | Yes      | Slack App > Settings > Basic Info > App-Level Tokens    |
| `bot_token`   | Yes      | Slack App > Settings > OAuth & Permissions              |
| `channel_id`  | Yes      | Right-click channel in Slack > View Details > bottom    |

**Slack App Setup:**
1. Create app at [api.slack.com/apps](https://api.slack.com/apps)
2. Enable **Socket Mode** (generates the `xapp-` app token)
3. Enable **Event Subscriptions** and subscribe to `message.channels`
4. Add `channels:history` and `channels:read` bot scopes
5. Install the app to your workspace (generates the `xoxb-` bot token)
6. Invite the bot to the channel where your status provider posts

### Webhook Provider (Event-Based)

For email-to-webhook bridges, Zapier, or any tool that can POST JSON:

```bash
curl -X POST http://localhost:8001/providers \
  -H "Content-Type: application/json" \
  -d '{
    "name": "PagerDuty",
    "source": "webhook",
    "config": {
      "path_prefix": "/hook/pagerduty"
    }
  }'
```

After onboarding, external tools POST events to:

```bash
curl -X POST http://localhost:8001/hook/PagerDuty \
  -H "Content-Type: application/json" \
  -d '{
    "status": "Database failover triggered",
    "message": "Primary DB unreachable. Failover to replica initiated.",
    "severity": "major"
  }'
```

### Managing Providers

```bash
# List all registered providers
curl http://localhost:8001/providers

# Remove a provider (stops its adapter immediately)
curl -X DELETE http://localhost:8001/providers/OpenAI
```

Provider configs persist to `providers.json` and survive restarts.

---

## How Polling is Optimized

The RSS adapter is the **fallback** for providers that lack a push channel. Even so, it is designed to be as close to event-driven as possible:

### Conditional HTTP Requests (ETag / If-Modified-Since)

```
First request:
  GET /history.atom
  -> 200 OK
  -> ETag: "abc123"
  -> Last-Modified: Thu, 20 Feb 2025 22:44:27 GMT

Subsequent requests (nothing changed):
  GET /history.atom
  If-None-Match: "abc123"
  If-Modified-Since: Thu, 20 Feb 2025 22:44:27 GMT
  -> 304 Not Modified        <-- no body transferred, near-zero bandwidth

Subsequent requests (new incident posted):
  GET /history.atom
  If-None-Match: "abc123"
  -> 200 OK                  <-- only then is the body parsed
  -> ETag: "def456"
```

**Result:** When nothing has changed (which is 99%+ of the time), the server returns a 3-byte `304` response. No feed body is transferred, no XML is parsed, no events are emitted. The cost per idle poll is essentially one TCP round-trip.

### Entry-Level De-duplication

Even when the feed body is returned, each entry's unique ID is tracked in a `seen_ids` set. Only genuinely new entries trigger an event emission. This prevents duplicate alerts when the server ignores conditional headers or when entries are reordered.

### Async Concurrency Model

```
100 RSS providers = 100 coroutines in ONE thread

Each coroutine lifecycle:
  1. send HTTP request        (~50ms network I/O, non-blocking)
  2. parse response           (~1ms CPU)
  3. await asyncio.sleep(60)  (~59.95s doing absolutely nothing)

CPU usage at idle: ~0%
Memory per provider: ~few KB (seen_ids set + connection state)
```

There are no threads per provider, no processes, no thread pool. The coroutines are multiplexed on a single event loop. Adding the 101st provider is just one more coroutine that spends 99.9% of its time in `await asyncio.sleep()`.

---

## System Advantages

### 1. Decoupled Architecture
- Components A and B can be deployed, scaled, and restarted independently
- The logging server can be swapped for any HTTP endpoint (database writer, Slack notifier, PagerDuty trigger) without touching the ingestion layer

### 2. Source Agnostic
- Three adapter types cover the vast majority of status page notification mechanisms
- Slack Socket Mode = truly event-driven, no public URL needed
- Webhooks = truly event-driven, works with any bridge tool
- RSS = smart polling fallback with near-zero idle cost

### 3. Runtime Flexibility
- Providers are onboarded and removed at runtime via REST API -- no restarts, no config file editing
- Configs persist to JSON and auto-load on restart

### 4. Security
- All events to the logging server require a valid `X-API-KEY` header
- Configurable via environment variable (`API_KEY`)

### 5. Scalability Profile
| Metric                  | Value                          |
|-------------------------|--------------------------------|
| Providers supported     | 100+ concurrently              |
| Threads per RSS provider| 0 (async coroutine)            |
| Threads per Slack adapter| 1 daemon thread               |
| Idle CPU usage          | Near zero                      |
| Idle bandwidth (RSS)    | ~3 bytes per poll (304)        |
| Memory per provider     | Few KB                         |

---

## Configuration Reference

| Env Var          | Default             | Component | Purpose                        |
|------------------|---------------------|-----------|--------------------------------|
| `API_KEY`        | `dev-secret-key`    | Both      | Shared secret for X-API-KEY    |
| `PORT`           | `8000`              | B         | Logging server listen port     |
| `BUS_PORT`       | `8001`              | A         | Ingestion bus listen port      |
| `LOG_SERVER_URL` | `http://localhost:8000` | A      | Where to POST events           |

---

## File Structure

```
/
  logging_server.py     # Component B -- FastAPI logging server
  ingestion_bus.py       # Component A -- Universal ingestion bus
  providers.json         # Persisted provider configurations
  onboard_examples.py    # Sample onboarding script
  requirements.txt       # Python dependencies
  README.md              # This file
```
