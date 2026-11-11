"""Corporate egress: the proxy stays in the process environment, and AWS_CA_BUNDLE is the
trust store for every HTTPS client dbtriage opens.

HTTP_PROXY, HTTPS_PROXY and NO_PROXY are not reimplemented here. urllib and httpx already
honor them, including NO_PROXY for the instance-metadata address, k3s service DNS, and the
LAN where local databases and NodePorts live. Database protocols are not HTTP, so Postgres,
Oracle, MySQL and SQL Server never go through that proxy. Do not set ALL_PROXY for this
process: some tools apply it to every socket, which would send those database connections
to the proxy as well.

The bundle the bank ships is the whole chain (corporate root and intermediates, plus the
public CAs for hosts that bypass the proxy). It is not replaced with certifi's public list.
"""
from __future__ import annotations

import os
import ssl
from urllib.parse import urlparse


def ca_bundle() -> str | None:
    """AWS_CA_BUNDLE when it is set. The path is returned even if it is missing, so a typo
    fails closed instead of quietly trusting the public roots."""
    path = os.environ.get("AWS_CA_BUNDLE")
    return path or None


def adopt_ca_bundle() -> None:
    """Point SDKs that read REQUESTS_CA_BUNDLE or SSL_CERT_FILE (Azure identity, some Google
    transports) at AWS_CA_BUNDLE when the operator has not chosen a different file."""
    ca = ca_bundle()
    if not ca:
        return
    if not os.environ.get("REQUESTS_CA_BUNDLE"):
        os.environ["REQUESTS_CA_BUNDLE"] = ca
    if not os.environ.get("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = ca


def ssl_context() -> ssl.SSLContext | None:
    ca = ca_bundle()
    if not ca:
        return None
    if not os.path.isfile(ca):
        raise OSError(f"TLS verification failed. AWS_CA_BUNDLE is not a file: {ca}")
    return ssl.create_default_context(cafile=ca)


def urlopen(req, timeout: float):
    """urllib with the corporate bundle when AWS_CA_BUNDLE is set. The default opener still
    applies HTTP(S)_PROXY and NO_PROXY."""
    import urllib.request

    ctx = ssl_context()
    if ctx is None:
        return urllib.request.urlopen(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def httpx_client():
    """An httpx client that trusts AWS_CA_BUNDLE, or None so the SDK keeps its own default.
    trust_env leaves the proxy variables to the process."""
    ca = ca_bundle()
    if not ca:
        return None
    if not os.path.isfile(ca):
        raise SystemExit(f"AWS_CA_BUNDLE is set but {ca} is not a file")
    adopt_ca_bundle()
    import httpx2

    return httpx2.Client(verify=ca, trust_env=True)


def proxy_configured() -> bool:
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))


def proxy_host() -> str | None:
    """Hostname only. A proxy URL often carries a username and password; those stay out of
    settings and /health."""
    raw = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
           or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "")
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else "//" + raw)
    return parsed.hostname or None


def no_proxy() -> str | None:
    return os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or None


def plain_provider_error(err: str) -> str:
    """One sentence for why a model call failed. An expired daily session is the normal
    morning, and must not be reported as a missing setup."""
    e = err.lower()
    if "expiredtoken" in e or "security token included in the request is expired" in e:
        return "AWS session expired. Copy today's keys from the access portal into ~/.aws/credentials."
    if "security token included in the request is invalid" in e or any(
            k in e for k in ("unable to locate credentials", "nocredentialserror", "could not resolve credentials")):
        return "No AWS credentials found."
    if any(k in e for k in ("certificate verify failed", "certificate_verify_failed", "sslcertverification", "aws_ca_bundle")):
        return "TLS verification failed. Set AWS_CA_BUNDLE to the corporate bundle."
    if not proxy_configured() and any(k in e for k in ("connecttimeout", "connect timeout", "connection timed out")):
        return "Could not reach the endpoint. This network may require HTTPS_PROXY."
    if any(k in e for k in ("authentication", "api_key", "api key", "credential", "unauthorized", "401", "403", "token")):
        return "No credentials configured for the model provider."
    if any(k in e for k in ("timed out", "timeout", "connection", "unreachable", "name resolution")):
        return "The model provider could not be reached."
    if "refus" in e or "declined" in e:
        return "The model declined the request."
    if "not found" in e or "404" in e or "model" in e and "exist" in e:
        return "The configured model was not found at the provider."
    return err.split("\n")[0][:140]
