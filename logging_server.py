"""
Component B: Logging Server
============================

A lightweight FastAPI server that receives normalized status events on POST /log,
validates an X-API-KEY header, and prints color-coded output to the console.

This server does NOT poll any external websites. It only listens to the
Universal Ingestion Bus (Component A).

Usage:
    python logging_server.py

Env vars (loaded from .env if present):
    API_KEY  -- shared secret for X-API-KEY auth (required)
    PORT     -- listen port (default: 8000)
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel
import uvicorn

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.environ["API_KEY"]
PORT = int(os.environ.get("PORT", "8000"))


# ---------------------------------------------------------------------------
# ANSI colors (no emojis, no external deps)
# ---------------------------------------------------------------------------

class _Color:
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    info = "info"
    major = "major"
    resolved = "resolved"


class StatusEvent(BaseModel):
    provider: str
    status: str
    message: str
    severity: Severity
    timestamp: str


# ---------------------------------------------------------------------------
# Lifespan + FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(application: FastAPI):
    print(
        f"{_Color.BOLD}Logging server started{_Color.RESET} "
        f"on port {PORT} -- POST /log to submit events",
        flush=True,
    )
    yield


app = FastAPI(
    title="Status Logging Server",
    description="Receives normalized status events and prints them to console.",
    version="1.0.0",
    lifespan=_lifespan,
)


def _verify_api_key(x_api_key: str | None) -> None:
    """Raise 401 if the API key is missing or incorrect."""
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _color_for_severity(severity: Severity) -> str:
    """Return the ANSI color code for a given severity level."""
    mapping = {
        Severity.major: _Color.RED,
        Severity.info: _Color.YELLOW,
        Severity.resolved: _Color.GREEN,
    }
    return mapping.get(severity, _Color.RESET)


def _severity_label(severity: Severity) -> str:
    """Return a plain-text label for the severity."""
    mapping = {
        Severity.major: "MAJOR",
        Severity.info: "INFO",
        Severity.resolved: "RESOLVED",
    }
    return mapping.get(severity, severity.value.upper())


def _print_event(event: StatusEvent) -> None:
    """Print a color-coded status event to stdout."""
    color = _color_for_severity(event.severity)
    label = _severity_label(event.severity)

    line = (
        f"{_Color.DIM}[{event.timestamp}]{_Color.RESET} "
        f"{color}{_Color.BOLD}[{label}]{_Color.RESET} "
        f"{_Color.CYAN}Provider:{_Color.RESET} {event.provider} | "
        f"{_Color.CYAN}Status:{_Color.RESET} {event.status} | "
        f"{event.message}"
    )
    print(line, flush=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/log")
async def log_event(
    event: StatusEvent,
    x_api_key: str | None = Header(None),
):
    """Receive a normalized status event, validate the API key, and log it."""
    _verify_api_key(x_api_key)
    _print_event(event)
    return {"status": "logged"}


@app.get("/health")
async def health():
    """Simple liveness probe."""
    return {"status": "ok"}





# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "logging_server:app",
        host="0.0.0.0",
        port=PORT,
        log_level="warning",
    )
