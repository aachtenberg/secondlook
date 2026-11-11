"""Laptop STS keys and cloud roles, as one AWS credential chain.

The bank portal (username, password, MFA) is the only issuer of the daily keys. dbtriage
does not log in, does not complete MFA, and does not store the keys. The person copies them
into ~/.aws/credentials, or exports AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and
AWS_SESSION_TOKEN. There is no refresh token. boto3 caches whatever it loaded on the first
call, so a long-running `dbtriage web` rebuilds its session when that file's mtime or those
three variables change. A workload role (instance, ECS, IRSA, Pod Identity) is the same
chain with no credentials file. Values are never logged.
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_fp: tuple | None = None
_clients: dict[tuple, object] = {}
_caller: dict = {"fp": None, "result": "not checked"}
_warned: set[str] = set()


def profile_name(cfg: dict | None = None) -> str | None:
    """[agent] aws_profile, else AWS_PROFILE. Blank means the default chain, so a cloud
    recipe with no profile keeps using the attached role."""
    raw = (cfg or {}).get("aws_profile")
    if isinstance(raw, str):
        raw = raw.strip()
    name = raw or os.environ.get("AWS_PROFILE") or ""
    return name or None


def credentials_path() -> Path:
    raw = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    return Path(raw) if raw else Path.home() / ".aws" / "credentials"


def _secret_sig() -> str:
    """Hash of the three key variables. The hash is only a cache key; it is never logged."""
    h = hashlib.sha256()
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        h.update(name.encode())
        h.update(b"=")
        h.update((os.environ.get(name) or "").encode())
        h.update(b"\0")
    return h.hexdigest()


def material_fingerprint() -> tuple:
    """Changes when the credentials file is replaced or the key environment variables change.
    Also when the CA bundle or the proxy changes, so clients built earlier pick that up too."""
    path = credentials_path()
    try:
        st = path.stat()
        file_sig = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        file_sig = (str(path), None, None)
    return (
        file_sig,
        _secret_sig(),
        os.environ.get("AWS_PROFILE") or "",
        os.environ.get("AWS_CA_BUNDLE") or "",
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "",
        os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "",
        os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "",
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "",
        os.environ.get("AWS_ENDPOINT_URL") or "",
    )


def note_credential_change() -> bool:
    """Drop cached AWS clients when the laptop pastes new keys. Returns True when they changed.
    Also clears the Anthropic Bedrock SDK's session cache, which would otherwise keep the
    first credentials for the life of the process."""
    global _fp
    fp = material_fingerprint()
    with _lock:
        if _fp == fp:
            return False
        _fp = fp
        _clients.clear()
    try:
        from anthropic.lib.bedrock._auth import _get_session

        _get_session.cache_clear()
    except Exception:  # noqa: BLE001 - the SDK cache is an extra; our own clients already rebuilt
        pass
    return True


def client(service: str, *, region: str | None = None, profile: str | None = None,
           endpoint_url: str | None = None, config=None):
    """A boto3 client for this service. Rebuilt when the credentials file or the key
    environment variables change, so the morning paste needs no restart."""
    from dbtriage.egress import ca_bundle

    note_credential_change()
    cfg_key = None
    if config is not None:
        cfg_key = (getattr(config, "connect_timeout", None), getattr(config, "read_timeout", None),
                   str(getattr(config, "retries", None)))
    key = (service, region or "", profile or "", endpoint_url or "", cfg_key)
    with _lock:
        hit = _clients.get(key)
        if hit is not None:
            return hit
        import boto3

        sess = boto3.Session(profile_name=profile or None)
        kw: dict = {}
        if region:
            kw["region_name"] = region
        if endpoint_url:
            kw["endpoint_url"] = endpoint_url
        if config is not None:
            kw["config"] = config
        ca = ca_bundle()
        if ca:
            kw["verify"] = ca
        made = sess.client(service, **kw)
        _clients[key] = made
        return made


def _profile_values(text: str, profile: str) -> dict[str, str]:
    current = None
    found = False
    vals: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(";"):
            continue
        if s.startswith("[") and s.endswith("]"):
            if found:
                break
            current = s[1:-1].strip()
            found = current == profile
            continue
        if found and "=" in s:
            k, v = s.split("=", 1)
            vals[k.strip().lower()] = v.strip()
    return vals


def session_token_present(profile: str | None) -> bool:
    """Whether the credentials boto3 will actually use include a session token.
    Environment keys win over the file, matching the SDK. The token value is not returned."""
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return bool(os.environ.get("AWS_SESSION_TOKEN"))
    path = credentials_path()
    try:
        text = path.read_text()
    except OSError:
        return False
    return bool(_profile_values(text, profile or "default").get("aws_session_token"))


def _age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 36 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _warn_mode(path: Path) -> None:
    key = str(path)
    if key in _warned:
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    _warned.add(key)
    if mode & 0o077:
        print(f"aws: {path} can be read by other users (mode {oct(mode & 0o777)[2:]}); it holds session keys: chmod 600 {path}",
              file=sys.stderr)


def note_caller(result) -> None:
    _caller["fp"] = material_fingerprint()
    _caller["result"] = result


def note_from_error(exc: BaseException) -> None:
    """Record an expired or missing session from an error we already have. No extra call."""
    msg = f"{type(exc).__name__}: {exc}".lower()
    if "expiredtoken" in msg or "security token included in the request is expired" in msg:
        note_caller("expired")
    elif "unable to locate credentials" in msg or "nocredentialserror" in msg:
        note_caller("none")


def caller_result():
    if _caller.get("fp") != material_fingerprint():
        return "not checked"
    return _caller.get("result", "not checked")


def probe_caller(profile: str | None, region: str | None):
    """GetCallerIdentity for Test model. Settings and /health only show the last result;
    they do not call STS. Short timeouts so a missing metadata service does not hang the button.
    The result is account and ARN, or 'expired' / 'none' / 'error', until the credentials change."""
    from botocore.config import Config

    region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    short = Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1})
    try:
        ident = client("sts", region=region, profile=profile, config=short).get_caller_identity()
        result = {"account": ident.get("Account") or "", "arn": ident.get("Arn") or ""}
    except Exception as exc:  # noqa: BLE001 - the status is the point; the raw error is not stored
        note_from_error(exc)
        if caller_result() == "not checked":
            note_caller("error")
        return caller_result()
    note_caller(result)
    return result


def describe(profile: str | None = None) -> dict:
    """What settings and /health may show. No key material, no proxy password."""
    from dbtriage.egress import ca_bundle, no_proxy, proxy_host

    path = credentials_path()
    _warn_mode(path)
    try:
        st = path.stat()
    except OSError:
        st = None
    age_s = int(max(0, time.time() - st.st_mtime)) if st else None
    return {
        "profile": profile,
        "credentials_file": str(path) if st else None,
        "credentials_age": _age(age_s) if age_s is not None else None,
        "credentials_age_s": age_s,
        "session_token": session_token_present(profile),
        "proxy": proxy_host(),
        "no_proxy": no_proxy(),
        "ca_bundle": ca_bundle(),
        "caller": caller_result(),
    }


def status_line(profile: str | None = None) -> str:
    d = describe(profile)
    bits = [f"AWS profile {d['profile'] or 'default chain'}"]
    bits.append(f"credentials file {d['credentials_age']} old" if d["credentials_file"] else "no credentials file")
    bits.append("session token present" if d["session_token"] else "no session token")
    bits.append(f"proxy {d['proxy']}" if d["proxy"] else "no HTTPS_PROXY")
    if d["no_proxy"]:
        bits.append(f"NO_PROXY {d['no_proxy']}")
    bits.append(f"CA {d['ca_bundle']}" if d["ca_bundle"] else "no AWS_CA_BUNDLE")
    caller = d["caller"]
    if isinstance(caller, dict):
        bits.append(f"caller {caller.get('account')} {caller.get('arn')}")
    else:
        bits.append(f"caller {caller}")
    return " · ".join(bits)


def reset() -> None:
    """Drop caches. Tests only."""
    global _fp
    with _lock:
        _fp = None
        _clients.clear()
        _caller["fp"] = None
        _caller["result"] = "not checked"
        _warned.clear()
    _caller["fp"] = None
    _caller["result"] = "not checked"
    _warned.clear()
