"""Opt-in policy, actual local TLS and real spawn/RPC regression evidence.

The local TLS fixture replaces DNS answers only inside trusted test code. It
does not add a private-host bypass to production code or persisted settings.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.agent import web_access
from core.agent.bot_manager import BotManager
from core.agent.capabilities import (
    CAPABILITY_CATALOG, POLICY_KEY, CapabilityCatalog, CapabilityConflict, CapabilityContext,
    CapabilityDefinition, CapabilityPolicyError, CapabilitySession, describe_capabilities,
    load_capability_snapshot, update_capability_policy,
)
from core.agent.engine import KillablePythonExecutor
from core.agent.tools import CloudCallTool, RunSkillTool
from core.agent.web_policy import WebAccessError, WebReadPolicy, canonical_domain, public_address, validate_web_url
from core.auth import CurrentUser
from core.persistence import SqliteWebRequestLimiter, WebRequestLimitError, open_database
from core.runtime_context import AuthScope, capability_cancellation, runtime_auth_scope, runtime_is_operator, runtime_user_id
from core.tim_client import TimClient


@pytest.fixture
def manager(tmp_path):
    return BotManager(data_dir=str(tmp_path / "data"))


@contextmanager
def identity(issuer="", tenant="", subject="local"):
    scope = runtime_auth_scope.set(AuthScope(issuer, tenant, subject))
    user = runtime_user_id.set(subject)
    try:
        yield
    finally:
        runtime_auth_scope.reset(scope)
        runtime_user_id.reset(user)


def enable_web(manager, **policy):
    old = load_capability_snapshot(manager)
    return update_capability_policy(
        manager, [], expected_version=old.version, catalog_version=old.catalog_version,
        enabled_ids=["web_read"], web_read={"allowed_domains": ["pages.example.test"], **policy},
    )


def test_default_off_in_fresh_and_legacy_policies_and_explicit_enable_required(manager):
    assert not load_capability_snapshot(manager).enabled("web_read")
    manager.settings_store.set(POLICY_KEY, {"schema_version": 1, "disabled_ids": []})
    old = load_capability_snapshot(manager)
    assert not old.enabled("web_read")
    updated = update_capability_policy(manager, [], expected_version=old.version, catalog_version=old.catalog_version)
    assert not updated.enabled("web_read")
    with pytest.raises(CapabilityPolicyError, match="域名"):
        update_capability_policy(manager, [], expected_version=updated.version,
                                 catalog_version=updated.catalog_version, enabled_ids=["web_read"])
    enabled = enable_web(manager)
    assert enabled.enabled("web_read")
    assert not old.enabled("web_read")
    # Legacy callers can still disable a newly enabled capability explicitly.
    disabled = update_capability_policy(manager, ["web_read"], expected_version=enabled.version,
                                       catalog_version=enabled.catalog_version)
    assert not disabled.enabled("web_read") and "web_read" not in disabled.enabled_ids


def test_generic_default_off_registration_and_fingerprint():
    first, second = CapabilityCatalog(), CapabilityCatalog()
    first.register(CapabilityDefinition("sample", "Sample", "test", default_enabled=False))
    second.register(CapabilityDefinition("sample", "Sample", "test", default_enabled=True))
    assert first.version != second.version


@pytest.mark.parametrize("data", [
    {"allowed_domains": "pages.example.test"}, {"allowed_domains": ["*.example.test"]},
    {"allowed_domains": ["https://example.test/a"]}, {"allowed_domains": ["127.0.0.1"]},
    {"allowed_domains": ["localhost"]}, {"allowed_domains": ["metadata.google.internal"]},
    {"allowed_domains": ["example.test", "EXAMPLE.test."]}, {"allowed_domains": [1]},
    {"allowed_domains": ["x.example.test"] * 65}, {"allowed_ports": [80]}, {"allowed_ports": [True]},
    {"allowed_ports": [443, 443]}, {"connect_timeout_seconds": True}, {"max_response_bytes": 2_097_153},
    {"max_text_chars": 0}, {"total_timeout_seconds": 31}, {"max_concurrent_requests": 9},
    {"requests_per_minute": 121}, {"connect_timeout_seconds": 5, "total_timeout_seconds": 2},
    {"proxy": "http://localhost:8080"}, {"headers": {"Authorization": "synthetic"}},
])
def test_policy_rejects_unsafe_or_malformed_settings(data):
    with pytest.raises(ValidationError):
        WebReadPolicy.model_validate(data)


def test_domain_canonicalization_and_frozen_policy():
    assert canonical_domain("Docs.Example.test.") == "docs.example.test"
    policy = WebReadPolicy(allowed_domains=["例子.测试"])
    assert policy.allowed_domains == ("xn--fsqu00a.xn--0zwm56d",)
    with pytest.raises(ValidationError):
        policy.max_text_chars = 123


@pytest.mark.parametrize("url", [
    "http://pages.example.test/a", "file:///etc/passwd", "https://127.0.0.1/", "https://[::1]/",
    "https://2130706433/", "https://pages.example.test.evil.test/", "https://sub.pages.example.test/",
    "https://pages.example.test@evil.test/", "https://u:p@pages.example.test/", "https://pages.example.test:0/",
    "https://pages.example.test:444/", "https://pages.example.test/#x", "https://pages.example.test/\r\nX:x",
    "https://pages.example.test\\@evil.test/", "https://pages.example.test//evil.test/", "https://pages.example.test/中文",
    [], None,
])
def test_url_bypasses_rejected(url):
    with pytest.raises(WebAccessError):
        validate_web_url(url, WebReadPolicy(allowed_domains=["pages.example.test"]))


def test_url_is_normalized_once_and_keeps_origin_form_target():
    policy = WebReadPolicy(allowed_domains=["pages.example.test"])
    assert validate_web_url("https://PAGES.example.test.:443/a?q=1", policy) == (
        "https://pages.example.test/a?q=1", "pages.example.test", "/a?q=1",
    )


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.100.100.200",
    "168.63.129.16", "0.0.0.0", "198.18.0.1", "224.0.0.1", "192.0.2.1", "::1", "::", "fe80::1",
    "fd00:ec2::254", "::ffff:8.8.8.8", "2002:0808:0808::1", "64:ff9b::808:808", "2606:4700::1%en0",
])
def test_private_metadata_reserved_and_transition_addresses_rejected(address):
    with pytest.raises(WebAccessError):
        public_address(address)


def test_every_dns_answer_is_validated_and_no_connection_precedes_validation(monkeypatch):
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
               (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
    monkeypatch.setattr(web_access.socket, "getaddrinfo", lambda *a, **k: answers)
    with pytest.raises(WebAccessError, match="禁止"):
        web_access._resolve_public_addresses("pages.example.test", 443)
    answers.pop()
    assert web_access._resolve_public_addresses("pages.example.test", 443) == [(socket.AF_INET, ("8.8.8.8", 443))]


def test_private_model_exceptions_do_not_apply_to_web_reads(monkeypatch):
    monkeypatch.setenv("TC_STUDIO_ALLOW_PRIVATE_MODEL_HOSTS", "pages.example.test")
    monkeypatch.setattr(web_access.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    monkeypatch.setattr(web_access.socket, "socket", lambda *a, **k: pytest.fail("connection before DNS approval"))
    with pytest.raises(WebAccessError, match="禁止"):
        web_access._fetch_page("https://pages.example.test/ok", WebReadPolicy(allowed_domains=["pages.example.test"]))


class PageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        body = "<html><title>测试页面</title><p>你好，TC Studio</p><script>evil()</script><style>hidden</style></html>".encode()
        status, mime, headers = 200, "text/html; charset=utf-8", {}
        if self.path.startswith("/redirect"):
            status, headers = 302, {"Location": "http://169.254.169.254/latest/meta-data/"}
        elif self.path.startswith("/compressed"):
            headers = {"Content-Encoding": "gzip"}
        elif self.path.startswith("/download"):
            headers = {"Content-Disposition": "attachment; filename=report.html"}
        elif self.path.startswith("/binary"):
            mime = "application/pdf"
        elif self.path.startswith("/large"):
            body = b"x" * 2048
        elif self.path.startswith("/truncate"):
            body = b"short"
        elif self.path.startswith("/headers"):
            headers = {"X-Large": "a" * 32768}
        elif self.path.startswith("/charset"):
            mime = "text/html; charset=utf-7"
        elif self.path.startswith("/text"):
            mime, body = "text/plain; charset=utf-8", ("a" * 200).encode()
        elif self.path.startswith("/slow"):
            time.sleep(4)
        self.send_response(status)
        if not self.path.startswith("/missing-type"):
            self.send_header("Content-Type", mime)
        chunked = self.path.startswith("/chunked")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        if not self.path.startswith("/large-stream") and (not chunked or self.path.startswith("/chunked-conflict")):
            self.send_header("Content-Length", str(100 if self.path.startswith("/truncate") else len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        with suppress(OSError):
            if self.path.startswith("/chunked-metadata"):
                self.wfile.write(b"1;" + b"x" * 32768 + b"\r\nx\r\n0\r\n\r\n")
            elif chunked:
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            else:
                self.wfile.write(body)


@pytest.fixture(scope="module")
def tls_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("web-tls")
    cert, key = root / "cert.pem", root / "key.pem"
    if not shutil.which("openssl"):
        pytest.skip("openssl is required to generate the local TLS test certificate")
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=pages.example.test",
        "-addext", "subjectAltName=DNS:pages.example.test",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    server.requests = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, cert
    server.shutdown()
    server.server_close()
    thread.join(2)


def _controlled_tls_worker(conn, liveness, url, policy_data):
    # Explicitly trusted fixture seam; the production policy has no bypass flag.
    from core.agent import web_access as module
    port = int(os.environ["TC_STUDIO_TEST_TLS_PORT"])
    module._resolve_public_addresses = lambda host, port_ignored: [(socket.AF_INET, ("127.0.0.1", port))]
    original_context = ssl.create_default_context
    module.ssl.create_default_context = lambda: original_context(cafile=os.environ["TC_STUDIO_TEST_TLS_CERT"])
    # Any accidental second DNS resolution must fail the TLS integration test.
    module.socket.getaddrinfo = lambda *a, **k: (_ for _ in ()).throw(AssertionError("second DNS lookup"))
    module._web_worker(conn, liveness, url, policy_data)


@pytest.fixture
def local_reader(tls_server, monkeypatch):
    server, cert = tls_server
    monkeypatch.setenv("TC_STUDIO_TEST_TLS_PORT", str(server.server_port))
    monkeypatch.setenv("TC_STUDIO_TEST_TLS_CERT", str(cert))
    read = web_access._read_in_worker
    def controlled(url, policy, deadline):
        return read(url, policy, deadline, _worker=_controlled_tls_worker)
    monkeypatch.setattr(web_access, "_read_in_worker", controlled)
    return controlled


def test_real_tls_pins_address_keeps_host_and_strips_active_html(local_reader, tls_server, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    result = local_reader("https://pages.example.test/ok", WebReadPolicy(allowed_domains=["pages.example.test"]), time.monotonic() + 5)
    assert result["title"] == "测试页面" and "你好，TC Studio" in result["text"]
    assert "evil" not in result["text"] and "hidden" not in result["text"]
    assert result["untrusted"] is True
    headers = tls_server[0].requests[-1][1]
    assert headers["Host"] == "pages.example.test" and headers["Accept-Encoding"] == "identity"
    assert not {"Authorization", "Cookie", "Proxy-Authorization"} & headers.keys()


@pytest.mark.parametrize("path,message", [
    ("redirect", "302"), ("compressed", "压缩"), ("download", "下载"), ("binary", "HTML"),
    ("large", "字节上限"), ("large-stream", "字节上限"), ("truncate", "提前断开"),
    ("headers", "响应头"), ("charset", "字符编码"), ("missing-type", "HTML"),
    ("chunked-conflict", "冲突"), ("chunked-metadata", "元数据"),
])
def test_actual_tls_response_guards(local_reader, path, message):
    with pytest.raises(WebAccessError, match=message):
        local_reader(f"https://pages.example.test/{path}",
                     WebReadPolicy(allowed_domains=["pages.example.test"], max_response_bytes=1024), time.monotonic() + 5)


def test_bad_tls_hostname_is_not_ignored(local_reader):
    with pytest.raises(WebAccessError, match="TLS"):
        local_reader("https://wrong.example.test/ok", WebReadPolicy(allowed_domains=["wrong.example.test"]), time.monotonic() + 5)


def _untrusted_tls_worker(conn, liveness, url, policy_data):
    from core.agent import web_access as module
    port = int(os.environ["TC_STUDIO_TEST_TLS_PORT"])
    module._resolve_public_addresses = lambda host, port_ignored: [(socket.AF_INET, ("127.0.0.1", port))]
    module._web_worker(conn, liveness, url, policy_data)


def test_untrusted_tls_certificate_is_rejected(tls_server, monkeypatch):
    monkeypatch.setenv("TC_STUDIO_TEST_TLS_PORT", str(tls_server[0].server_port))
    with pytest.raises(WebAccessError, match="TLS"):
        web_access._read_in_worker("https://pages.example.test/ok", WebReadPolicy(allowed_domains=["pages.example.test"]),
                                   time.monotonic() + 5, _worker=_untrusted_tls_worker)


def test_chunked_html_uses_the_same_bounded_text_path(local_reader):
    result = local_reader("https://pages.example.test/chunked", WebReadPolicy(allowed_domains=["pages.example.test"]), time.monotonic() + 5)
    assert "你好" in result["text"] and result["untrusted"]


def test_plain_text_truncation_is_explicit(local_reader):
    result = local_reader("https://pages.example.test/text", WebReadPolicy(allowed_domains=["pages.example.test"], max_text_chars=100), time.monotonic() + 5)
    assert result["text"] == "a" * 100 and result["truncated"] is True and result["bytes_read"] == 200


def test_total_deadline_kills_and_reaps_slow_network_worker(local_reader):
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(WebAccessError, match="超时"):
        local_reader("https://pages.example.test/slow", WebReadPolicy(allowed_domains=["pages.example.test"]), started + 1)
    assert time.monotonic() - started < 2.5
    assert {p.pid for p in multiprocessing.active_children()} == before


def _blocked_dns_worker(conn, liveness, url, policy_data):
    from core.agent import web_access as module
    def blocked(*args, **kwargs):
        time.sleep(60)
        return []
    module.socket.getaddrinfo = blocked
    module._web_worker(conn, liveness, url, policy_data)


def test_dns_resolution_is_inside_the_killable_deadline():
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(WebAccessError, match="超时"):
        web_access._read_in_worker("https://pages.example.test/ok", WebReadPolicy(allowed_domains=["pages.example.test"]),
                                   started + 0.8, _worker=_blocked_dns_worker)
    assert time.monotonic() - started < 2
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_worker_exits_when_parent_liveness_pipe_is_lost():
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    alive_read, alive_write = context.Pipe(duplex=False)
    process = context.Process(target=_blocked_dns_worker, args=(child, alive_read, "https://pages.example.test/ok",
                                                               WebReadPolicy(allowed_domains=["pages.example.test"]).model_dump(mode="json")))
    process.start()
    child.close()
    alive_read.close()
    try:
        alive_write.close()
        process.join(3)
        assert not process.is_alive() and process.exitcode != 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(2)
        parent.close()
        process.close()


def test_cancellation_kills_and_reaps_network_worker(local_reader):
    before = {p.pid for p in multiprocessing.active_children()}
    stopped = threading.Event()
    timer = threading.Timer(0.7, stopped.set)
    timer.start()
    try:
        with capability_cancellation(stopped.is_set), pytest.raises(WebAccessError, match="取消"):
            local_reader("https://pages.example.test/slow", WebReadPolicy(allowed_domains=["pages.example.test"]), time.monotonic() + 5)
    finally:
        timer.cancel()
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_real_agent_stop_terminates_pending_host_network_call(manager, local_reader, tls_server):
    enable_web(manager)
    errors = []
    before = {process.pid for process in multiprocessing.active_children()}
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient())) as session:
        executor = KillablePythonExecutor([], force_spawn=True, timeout_seconds=3)
        executor.additional_functions = {"read_web_page": session.scope["read_web_page"]}
        executor.send_tools({})
        def execute():
            try:
                executor("read_web_page('https://pages.example.test/slow?real-stop-probe')")
            except Exception as exc:
                errors.append(type(exc).__name__)
        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not any(path == "/slow?real-stop-probe" for path, _ in tls_server[0].requests):
                assert time.monotonic() < deadline, "local TLS request did not start"
                threading.Event().wait(0.02)
            # This is the method registered in AgentRun's stop hook.
            executor.terminate()
            thread.join(1.5)
            assert not thread.is_alive(), "stopping evaluator left host network request running"
        finally:
            executor.close()
            thread.join(3)
    assert errors
    assert {process.pid for process in multiprocessing.active_children()} == before


@pytest.mark.parametrize("kind", ["agent", "delegate", "skill"])
def test_actual_rpc_binding_for_every_context(manager, local_reader, kind):
    enable_web(manager)
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient(), kind=kind)) as session:
        if kind == "skill":
            runner = RunSkillTool("default_assistant", manager, TimClient(), capability_snapshot=session.snapshot)
            result = runner._run_isolated("result = read_web_page('https://pages.example.test/ok')", {}, CloudCallTool(), session.snapshot)
            assert result["success"] and "你好" in result["result"]["text"]
        else:
            executor = KillablePythonExecutor([], force_spawn=True, timeout_seconds=3)
            executor.additional_functions = {"read_web_page": session.scope["read_web_page"]}
            try:
                executor.send_tools({})
                result = executor("read_web_page('https://pages.example.test/ok')['text']")
                assert "你好" in result.output
            finally:
                executor.close()


def test_bound_reader_rejects_identity_and_operator_change(manager):
    with identity("issuer-a", "tenant-a", "alice"):
        enable_web(manager)
        with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient())) as session:
            read = session.scope["read_web_page"]
            with identity("issuer-a", "tenant-a", "bob"), pytest.raises(WebAccessError, match="当前用户"):
                read("https://pages.example.test/ok")
            token = runtime_is_operator.set(False)
            try:
                with pytest.raises(WebAccessError, match="授权"):
                    read("https://pages.example.test/ok")
            finally:
                runtime_is_operator.reset(token)
    with identity("issuer-b", "tenant-a", "alice"):
        assert not load_capability_snapshot(manager).enabled("web_read")


def test_network_snapshot_updates_are_immutable_and_audit_omits_query(manager, local_reader):
    enable_web(manager)
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient())) as session:
        old = session.snapshot
        enable_web(manager, allowed_domains=["new.example.test"])
        result = session.scope["read_web_page"]("https://pages.example.test/ok?secret=synthetic-sensitive-query")
        assert result["untrusted"]
        assert old.web_read.allowed_domains == ("pages.example.test",)
        assert load_capability_snapshot(manager).web_read.allowed_domains == ("new.example.test",)
    audits = manager.audit_store.list_recent()
    event = next(item for item in audits if item["action"] == "agent.web_read")
    assert event["actor_subject"] == "local" and event["outcome"] == "SUCCESS"
    assert "synthetic-sensitive-query" not in json.dumps(event)
    connection = open_database(manager.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM web_request_leases").fetchone()[0] == 0
    finally:
        connection.close()


def test_failed_network_request_releases_lease_and_records_bounded_audit(manager, local_reader):
    enable_web(manager)
    with CapabilitySession(CapabilityContext("default_assistant", manager, TimClient())) as session:
        with pytest.raises(WebAccessError, match="302"):
            session.scope["read_web_page"]("https://pages.example.test/redirect?sensitive=do-not-log")
    event = next(item for item in manager.audit_store.list_recent() if item["action"] == "agent.web_read")
    assert event["outcome"] == "ERROR" and "do-not-log" not in json.dumps(event)
    connection = open_database(manager.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM web_request_leases").fetchone()[0] == 0
    finally:
        connection.close()


def _reload_network_policy(path, conn):
    from types import SimpleNamespace
    from core.persistence import SqliteAppSettingsStore
    manager = SimpleNamespace(database_path=path, settings_store=SqliteAppSettingsStore(path))
    old = load_capability_snapshot(manager)
    conn.send(describe_capabilities(old))
    conn.recv()
    fresh = describe_capabilities(load_capability_snapshot(manager))
    conn.send((fresh, old.enabled("web_read")))
    conn.close()


def test_another_running_process_reads_web_policy_without_restart(manager):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_reload_network_policy, args=(str(manager.database_path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv()["enabled_ids"] == []
        enable_web(manager)
        parent.send("reload")
        assert parent.poll(10)
        fresh, old_enabled = parent.recv()
        assert fresh["enabled_ids"] == ["web_read"] and not old_enabled
        assert fresh["web_read"]["allowed_domains"] == ["pages.example.test"]
    finally:
        process.join(3)
        if process.is_alive():
            process.kill()
            process.join(2)
        parent.close()
    assert process.exitcode == 0


def test_http_requires_admin_validates_policy_and_isolates_issuer(manager, monkeypatch):
    import app as app_module
    import apis.agent as routes
    import core.auth as auth
    user = CurrentUser("reader", frozenset({"operator"}), claims={"iss": "issuer-a", "tenant_id": "tenant-a"})
    monkeypatch.setattr(routes, "bot_manager", manager)
    monkeypatch.setattr(auth, "authenticate_request", lambda request: user)
    client = TestClient(app_module.app)
    assert client.get("/api/agent/capabilities").status_code == 403
    user = CurrentUser("admin", frozenset({"admin"}), claims={"iss": "issuer-a", "tenant_id": "tenant-a"})
    first = client.get("/api/agent/capabilities").json()
    web = next(item for item in first["capabilities"] if item["id"] == "web_read")
    assert web["default_enabled"] is False and not any(web["enabled"].values())
    payload = {"expected_version": first["version"], "catalog_version": first["catalog_version"],
               "disabled_ids": [], "enabled_ids": ["web_read"], "web_read": {"allowed_domains": ["pages.example.test"]}}
    assert client.put("/api/agent/capabilities", json={**payload, "web_read": {"allowed_ports": [80]}}).status_code == 422
    assert client.put("/api/agent/capabilities", json={**payload, "web_read": {"proxy": "http://localhost"}}).status_code == 422
    assert client.put("/api/agent/capabilities", json={**payload, "web_read": {}}).status_code == 422
    response = client.put("/api/agent/capabilities", json=payload)
    assert response.status_code == 200 and response.json()["enabled_ids"] == ["web_read"]
    assert client.put("/api/agent/capabilities", json=payload).status_code == 409
    user = CurrentUser("admin", frozenset({"admin"}), claims={"iss": "issuer-b", "tenant_id": "tenant-a"})
    other = client.get("/api/agent/capabilities").json()
    assert other["enabled_ids"] == [] and other["web_read"]["allowed_domains"] == []


def _limiter_contender(path, start, release, conn):
    limiter = SqliteWebRequestLimiter(path)
    start.wait(10)
    lease = None
    try:
        lease = limiter.acquire(issuer="i", tenant_id="t", subject="u", concurrency=2, per_minute=120, lifetime=10)
        conn.send("admitted")
        release.wait(10)
    except WebRequestLimitError:
        conn.send("blocked")
    finally:
        if lease:
            limiter.release(lease)
        conn.close()


def test_sqlite_limits_are_shared_by_real_service_processes(manager):
    context = multiprocessing.get_context("spawn")
    start, release = context.Event(), context.Event()
    children, parents = [], []
    try:
        for _ in range(4):
            parent, child = context.Pipe()
            process = context.Process(target=_limiter_contender, args=(str(manager.database_path), start, release, child))
            process.start()
            child.close()
            children.append(process)
            parents.append(parent)
        start.set()
        assert all(parent.poll(15) for parent in parents)
        results = [parent.recv() for parent in parents]
        assert results.count("admitted") == 2 and results.count("blocked") == 2
        # An active network lease holds no database write lock.
        manager.settings_store.set("test.concurrent.setting", {"ok": True})
    finally:
        release.set()
        for process in children:
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(2)
        for parent in parents:
            parent.close()
    assert all(process.exitcode == 0 for process in children)


def test_rate_quota_failure_cleanup_expiry_and_global_hard_ceiling(manager):
    limiter = SqliteWebRequestLimiter(str(manager.database_path))
    args = dict(issuer="a", tenant_id="t", subject="alice", concurrency=8, per_minute=1, lifetime=10)
    lease = limiter.acquire(**args)
    limiter.release(lease)
    with pytest.raises(WebRequestLimitError, match="每分钟"):
        limiter.acquire(**args)
    leases = []
    try:
        for index in range(8):
            leases.append(limiter.acquire(**{**args, "issuer": f"issuer-{index}"}))
        with pytest.raises(WebRequestLimitError, match="并发"):
            limiter.acquire(**{**args, "issuer": "issuer-extra"})
        connection = open_database(manager.database_path)
        try:
            connection.execute("UPDATE web_request_leases SET expires_at = 0 WHERE lease_id = ?", (leases[0],))
        finally:
            connection.close()
        leases.append(limiter.acquire(**{**args, "issuer": "issuer-extra"}))
    finally:
        for item in leases:
            limiter.release(item)


def test_malformed_persisted_web_policy_fails_closed(manager):
    manager.settings_store.set(POLICY_KEY, {"schema_version": 2, "disabled_ids": [], "enabled_ids": ["web_read"],
                                           "web_read": {"allowed_domains": ["pages.example.test"], "follow_redirects": True}})
    with pytest.raises(CapabilityPolicyError, match="持久化"):
        load_capability_snapshot(manager)
