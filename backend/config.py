"""Standalone configuration for the protocol registration tool."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "runtime" / "data"
AUTH_FILE = DATA_DIR / "auth.json"

MOEMAIL_API_KEY = os.getenv("GROK2API_MOEMAIL_API_KEY", os.getenv("MOEMAIL_API_KEY", ""))
MOEMAIL_BASE_URL = os.getenv("GROK2API_MOEMAIL_BASE_URL", os.getenv("MOEMAIL_BASE_URL", "https://maliapi.215.im"))
MOEMAIL_DOMAIN = os.getenv("GROK2API_MOEMAIL_DOMAIN", os.getenv("MOEMAIL_DOMAIN", ""))
MOEMAIL_EXPIRY_MS = int(os.getenv("GROK2API_MOEMAIL_EXPIRY_MS", os.getenv("MOEMAIL_EXPIRY_MS", "86400000")))

XAI_PROXY = os.getenv("GROK2API_XAI_PROXY", "")
XAI_PROXY_USERNAME = os.getenv("GROK2API_XAI_PROXY_USERNAME", "")
XAI_PROXY_PASSWORD = os.getenv("GROK2API_XAI_PROXY_PASSWORD", "")
XAI_PROXY_STRATEGY = os.getenv("GROK2API_XAI_PROXY_STRATEGY", "round_robin")
UPSTREAM_BASE = os.getenv("GROK2API_UPSTREAM_BASE", "https://grok.com")

REGISTRATION_TARGET = os.getenv("PROGROK_REGISTRATION_TARGET", "grok").strip().lower()
if REGISTRATION_TARGET not in ("grok", "chatgpt"):
    REGISTRATION_TARGET = "grok"

GROK_CLI_CLIENT_ID = os.getenv("GROK2API_OIDC_CLIENT_ID", "b1a00492-073a-47ea-816f-4c329264a828")
OIDC_ISSUER = os.getenv("GROK2API_OIDC_ISSUER", "https://auth.x.ai")
OIDC_SCOPES = os.getenv(
    "GROK2API_OIDC_SCOPES",
    "openid profile email offline_access grok-cli:access api:access conversations:read conversations:write",
)
