"""Gemini and Azure OpenAI without a pasted key.

Gemini refreshes application default credentials (a service account, workload identity,
or a user ADC file). Azure OpenAI uses DefaultAzureCredential when no API key is set.
A token printed by api_key_cmd stays a one-shot on purpose: that command is whatever
targets.toml names, and it is not run again on every request.
"""
from __future__ import annotations

import os
import threading

_lock = threading.Lock()
_gemini = None
_gemini_sig: tuple | None = None
_azure = None

_GEMINI_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_AZURE_SCOPE = "https://cognitiveservices.azure.com/.default"


def _google_request():
    from dbtriage.egress import adopt_ca_bundle, ca_bundle

    adopt_ca_bundle()
    import requests
    from google.auth.transport.requests import Request

    session = requests.Session()
    if ca := ca_bundle():
        session.verify = ca
    return Request(session)


def gemini_access_token() -> str:
    """A bearer token for Vertex Gemini. GOOGLE_OAUTH_ACCESS_TOKEN still wins. Otherwise
    application default credentials, refreshed when they expire."""
    if tok := os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
        return tok
    global _gemini, _gemini_sig
    sig = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "",)
    with _lock:
        if _gemini is None or _gemini_sig != sig:
            import google.auth

            try:
                creds, _ = google.auth.default(scopes=[_GEMINI_SCOPE])
            except Exception as exc:  # noqa: BLE001 - the message is the configuration error
                raise SystemExit(
                    "[agent] no Google application default credentials "
                    f"({type(exc).__name__}: {exc}). Set GOOGLE_OAUTH_ACCESS_TOKEN, or run where a "
                    "service account or workload identity is attached. A one-shot command can still "
                    "be set as api_key_cmd in targets.toml.") from exc
            _gemini, _gemini_sig = creds, sig
        creds = _gemini
    if not getattr(creds, "valid", False):
        creds.refresh(_google_request())
    token = getattr(creds, "token", None)
    if not token:
        raise SystemExit("[agent] Google application default credentials produced no access token")
    return token


def azure_access_token() -> str:
    """A bearer token for Azure OpenAI from DefaultAzureCredential (managed identity,
    workload identity, or az login). The azure extra supplies the library."""
    global _azure
    from dbtriage.egress import adopt_ca_bundle

    adopt_ca_bundle()
    with _lock:
        if _azure is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError:
                raise SystemExit("[agent] Azure OpenAI without a key needs the azure extra: "
                                 "install dbtriage[azure] (azure-identity)") from None
            _azure = DefaultAzureCredential()
        cred = _azure
    return cred.get_token(_AZURE_SCOPE).token


def reset() -> None:
    global _gemini, _gemini_sig, _azure
    with _lock:
        _gemini = None
        _gemini_sig = None
        _azure = None
