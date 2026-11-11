"""Laptop portal keys, corporate egress, cloud roles, and the trusted-proxy actor."""
import base64
import json
import os
import threading
import urllib.request
from urllib.error import HTTPError

import pytest

from dbtriage import access, awscred, cloudauth
from dbtriage.egress import plain_provider_error, proxy_host, urlopen
from dbtriage.web import agent_reason
from tests.conftest import free_port, wait_for
from tests.test_web import _req, config, store


def _drop_later(monkeypatch, name: str) -> None:
    """Remember to delete an env var a test may create. delenv does not, when it is already absent."""
    if name in os.environ:
        return
    monkeypatch.setenv(name, "")
    del os.environ[name]


def _quiet_aws(monkeypatch, tmp_path):
    """Keep these tests off the real credentials file and off a real proxy."""
    cred = tmp_path / "credentials"
    cred.write_text("[dev]\naws_access_key_id = AKIATEST\naws_secret_access_key = secret\naws_session_token = tok\n")
    cred.chmod(0o600)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(cred))
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
                 "AWS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
                 "NO_PROXY", "no_proxy", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        monkeypatch.delenv(name, raising=False)
    awscred.reset()
    cloudauth.reset()
    return cred


def test_agent_reason_keeps_the_old_sentences_and_names_the_new_ones(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    assert agent_reason('TypeError: "Could not resolve authentication method. Expected one of api_key"') == "No credentials configured for the model provider."
    assert agent_reason("ConnectError: connection refused") == "The model provider could not be reached."
    assert agent_reason("weird\nsecond line") == "weird"
    assert agent_reason("x" * 200) == "x" * 140
    expired = "ExpiredTokenException: The security token included in the request is expired"
    assert agent_reason(expired) == "AWS session expired. Copy today's keys from the access portal into ~/.aws/credentials."
    assert plain_provider_error("NoCredentialsError: Unable to locate credentials") == "No AWS credentials found."
    assert plain_provider_error("SSLCertVerificationError: certificate verify failed") == "TLS verification failed. Set AWS_CA_BUNDLE to the corporate bundle."
    assert plain_provider_error("ConnectTimeoutError: connection timed out") == "Could not reach the endpoint. This network may require HTTPS_PROXY."
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    assert plain_provider_error("ConnectTimeoutError: connection timed out") == "The model provider could not be reached."


def test_proxy_host_drops_userinfo(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:s3cret-proxy@proxy.example:8080")
    assert proxy_host() == "proxy.example"


def test_describe_has_no_secret(monkeypatch, tmp_path):
    _quiet_aws(monkeypatch, tmp_path)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value-xyz")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token-value-xyz")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:s3cret-proxy@proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,169.254.169.254,.svc")
    blob = json.dumps(awscred.describe("dev"))
    assert "super-secret-value-xyz" not in blob and "session-token-value-xyz" not in blob and "s3cret-proxy" not in blob
    assert "proxy.example" in blob and "169.254.169.254" in blob
    line = awscred.status_line("dev")
    assert "super-secret-value-xyz" not in line and "session token present" in line


def test_session_rebuilds_when_the_file_or_the_env_changes(monkeypatch, tmp_path):
    cred = _quiet_aws(monkeypatch, tmp_path)
    made = []

    class FakeSession:
        def __init__(self, profile_name=None):
            made.append(profile_name)

        def client(self, service, **kw):
            return (service, kw.get("region_name"))

    monkeypatch.setattr("boto3.Session", FakeSession)
    assert awscred.client("sts", region="us-east-1", profile="dev") == ("sts", "us-east-1")
    assert awscred.client("sts", region="us-east-1", profile="dev") == ("sts", "us-east-1")
    assert made == ["dev"]
    os.utime(cred, (cred.stat().st_atime, cred.stat().st_mtime + 5))
    awscred.client("sts", region="us-east-1", profile="dev")
    assert made == ["dev", "dev"]
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok")
    awscred.client("sts", region="us-east-1", profile="dev")
    assert len(made) == 3
    awscred.reset()


def test_converse_profile_blank_means_the_default_chain(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    seen = []

    def fake(service, **kw):
        seen.append(kw.get("profile"))
        return object()

    monkeypatch.setattr("dbtriage.awscred.client", fake)
    from dbtriage.llm.bedrock_converse import BedrockConverseBackend

    BedrockConverseBackend({"model": "m", "region": "us-east-1", "aws_profile": "dev"})
    BedrockConverseBackend({"model": "m", "region": "us-east-1", "aws_profile": ""})
    assert seen == ["dev", None]
    assert awscred.profile_name({"aws_profile": ""}) is None


def test_urlopen_and_httpx_use_the_bundle(monkeypatch, tmp_path):
    ca = tmp_path / "bundle.pem"
    ca.write_text("not a pem")
    monkeypatch.setenv("AWS_CA_BUNDLE", str(ca))
    _drop_later(monkeypatch, "REQUESTS_CA_BUNDLE")
    _drop_later(monkeypatch, "SSL_CERT_FILE")
    seen = {}

    def fake_ctx(cafile=None):
        seen["ca"] = cafile
        return "CTX"

    monkeypatch.setattr("ssl.create_default_context", fake_ctx)

    def fake_open(req, timeout=None, context=None):
        seen["context"] = context
        return "opened"

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    assert urlopen("https://example", 1) == "opened"
    assert seen["ca"] == str(ca) and seen["context"] == "CTX"

    class FakeClient:
        def __init__(self, verify=None, trust_env=None):
            self.verify, self.trust_env = verify, trust_env

    monkeypatch.setattr("httpx2.Client", FakeClient)
    from dbtriage.egress import httpx_client

    client = httpx_client()
    assert client.verify == str(ca) and client.trust_env is True
    assert os.environ["REQUESTS_CA_BUNDLE"] == str(ca) and os.environ["SSL_CERT_FILE"] == str(ca)


def _bearer(client):
    """OpenAI stores a callable key as a provider and reports api_key as ''."""
    provider = getattr(client, "_api_key_provider", None)
    return provider() if provider else client.api_key


def test_gemini_uses_adc_unless_a_command_is_set(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    cloudauth.reset()
    calls = []

    class Creds:
        valid = True
        token = "g-tok"

    def fake_default(scopes=None):
        calls.append(scopes)
        return Creds(), "proj"

    monkeypatch.setattr("google.auth.default", fake_default)
    from dbtriage.llm.openai_backend import OpenAIBackend

    backend = OpenAIBackend({"provider": "vertex-gemini", "model": "gemini-test", "project_id": "p", "region": "global"})
    assert _bearer(backend.client) == "g-tok" and calls

    def fail(*_a, **_k):
        raise AssertionError("adc should not run when api_key_cmd is set")

    monkeypatch.setattr("google.auth.default", fail)
    cloudauth.reset()
    keyed = OpenAIBackend({"provider": "vertex-gemini", "model": "gemini-test", "project_id": "p", "api_key_cmd": "echo tok-123"})
    assert keyed.client.api_key == "tok-123"


def test_azure_openai_uses_a_bearer_and_names_the_extra(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    cloudauth.reset()

    class Tok:
        token = "az-tok"

    class Cred:
        def get_token(self, scope):
            assert scope == "https://cognitiveservices.azure.com/.default"
            return Tok()

    import azure.identity
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: Cred())
    from dbtriage.llm.openai_backend import OpenAIBackend

    backend = OpenAIBackend({"provider": "azure-openai", "model": "gpt", "base_url": "https://example.openai.azure.com/openai/v1/"})
    assert _bearer(backend.client) == "az-tok"

    cloudauth.reset()
    real_import = __import__

    def boom(name, *a, **k):
        if name == "azure.identity":
            raise ImportError("no azure")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", boom)
    with pytest.raises(SystemExit, match=r"dbtriage\[azure\]"):
        cloudauth.azure_access_token()


def test_settings_page_names_a_profile_and_has_no_key_fields(monkeypatch, tmp_path):
    _quiet_aws(monkeypatch, tmp_path)
    from dbtriage.web import App

    app = App(config(tmp_path), store(tmp_path), False, None, allow_edit=True)
    page = app.settings_page().decode()
    agent = page.split("<h2>Agent")[1].split("<h2>Delivery")[0]
    assert 'name="aws_profile"' in agent
    assert 'name="aws_access_key"' not in agent and 'name="aws_secret' not in agent and 'name="aws_session_token"' not in agent
    assert "no HTTPS_PROXY" in agent or "proxy " in agent
    assert "super-secret" not in page


def test_health_aws_is_cached_and_hidden_without_the_password(monkeypatch, tmp_path):
    _quiet_aws(monkeypatch, tmp_path)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value-xyz")

    def boom(*_a, **_k):
        raise AssertionError("health must not call STS")

    monkeypatch.setattr(awscred, "probe_caller", boom)
    from dbtriage.web import make_server

    srv = make_server(config(tmp_path), store(tmp_path), "127.0.0.1", free_port(), False, "s3cret", False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        wait_for(url + "/health")
        code, body = _req(url + "/health", auth=False)
        assert code == 200 and set(json.loads(body)) == {"ok", "store_ok", "settings_ok", "spooled"}
        code, body = _req(url + "/health")
        aws = json.loads(body)["aws"]
        assert aws["caller"] == "not checked" and aws["session_token"] in (True, False)
        assert "super-secret-value-xyz" not in body.decode()
    finally:
        srv.shutdown()


def test_trust_proxy_groups_and_audit(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DBTRIAGE_TRUST_PROXY", raising=False)
    from dbtriage.web import make_server

    srv = make_server(config(tmp_path), store(tmp_path), "127.0.0.1", free_port(), True, "s3cret", True, True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        wait_for(url + "/health")
        forged = {"X-Forwarded-User": "eve", "X-Forwarded-Groups": "dbas"}
        # Off: a browser header is not an actor, and editing still follows the process flag.
        code, _ = _req(url + "/settings/agent", "POST", b"provider=ollama&model=q", forged)
        assert code == 200
        err = capsys.readouterr().out
        assert "audit: x settings /settings/agent" in err and "audit: eve " not in err

        monkeypatch.setenv("DBTRIAGE_TRUST_PROXY", "1")
        monkeypatch.setenv("DBTRIAGE_GROUP_EDIT", "dbas")
        monkeypatch.setenv("DBTRIAGE_GROUP_RUN", "runners")
        monkeypatch.setenv("DBTRIAGE_GROUP_VIEW", "viewers")
        monkeypatch.setenv("DBTRIAGE_GROUP_CHAT", "chatters")
        assert _req(url + "/", headers=forged)[0] == 403  # eve is not in viewers
        assert set(json.loads(_req(url + "/health", auth=False)[1])) == {"ok", "store_ok", "settings_ok", "spooled"}
        viewer = {"X-Forwarded-User": "ada", "X-Forwarded-Groups": "viewers"}
        assert _req(url + "/", headers=viewer)[0] == 200
        assert _req(url + "/settings/agent", "POST", b"provider=ollama&model=q", viewer)[0] == 403
        editor = {"X-Forwarded-User": "ada", "X-Forwarded-Groups": "viewers,dbas"}
        code, body = _req(url + "/settings/agent", "POST", b"provider=ollama&model=q", editor)
        assert code == 200 and b"agent settings saved" in body
        assert "audit: ada settings /settings/agent" in capsys.readouterr().out
        assert _req(url + "/api/run", "POST", b'{"target":"demo","mode":"adhoc"}', {"Content-Type": "application/json", **editor})[0] == 403
        assert access.actor_from({"X-Forwarded-User": "ada\r\nline"}, "").name == "ada line"
        monkeypatch.setenv("DBTRIAGE_TRUST_PROXY", "1")
        runner = {"X-Forwarded-User": "ada", "X-Forwarded-Groups": "viewers,runners", "Content-Type": "application/json"}
        code, body = _req(url + "/api/run", "POST", b'{"target":"demo","mode":"adhoc","reason":"because"}', runner)
        assert code == 202
        assert "audit: ada run demo adhoc" in capsys.readouterr().out
        assert _req(url + "/api/snapshots/postgres/demo/2026-11-05/20261105T100000Z/chat/review", "POST",
                    b'{"question":"hi"}', {"Content-Type": "application/json", **editor})[0] == 403
    finally:
        srv.shutdown()
        access.clear_current()


def test_schedule_is_not_stopped_by_a_click_group(monkeypatch, tmp_path):
    monkeypatch.setenv("DBTRIAGE_TRUST_PROXY", "1")
    monkeypatch.setenv("DBTRIAGE_GROUP_RUN", "runners")
    access.set_current(access.Actor("ada", frozenset()))
    captured = {}

    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):
            captured["req"] = args[0]

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr("dbtriage.web.threading.Thread", FakeThread)
    from dbtriage.web import App

    app = App(config(tmp_path), store(tmp_path), True, None, allow_edit=True)
    code, _ = app.start_run("demo", "scheduled", "tick", True, source="schedule")
    assert code == 202 and captured["req"].actor == ""
    code, out = app.start_run("demo", "adhoc", "click", True)
    assert code == 403 and "group" in out["error"]
    access.set_current(access.Actor("ada", frozenset({"runners"})))
    code, _ = app.start_run("demo", "adhoc", "click", True)
    assert code == 202 and captured["req"].actor == "ada"
    access.clear_current()


def test_snapshot_source_shows_the_actor(tmp_path):
    from dbtriage.web import App

    st = store(tmp_path)
    sid = "postgres/demo/2026-11-05/20261105T100000Z"
    m = st.get_json(f"{sid}/manifest.json")
    m["trigger"] = {"source": "web", "reason": "because", "actor": "ada"}
    st.put_json(f"{sid}/manifest.json", m)
    page = App(config(tmp_path), st, False, None).target("demo", 0).decode()
    assert "web · ada · because" in page


def test_markdown_uses_the_expired_sentence():
    from dbtriage.report import render_markdown

    md = render_markdown({"target": "demo", "engine": "postgres", "severity": "info", "findings": [], "snapshot": "s",
                          "agent_error": "ExpiredToken: The security token included in the request is expired"})
    assert "AWS session expired. Copy today's keys from the access portal into ~/.aws/credentials." in md
    assert "ExpiredToken" not in md
