"""The active-run limit is per complete user identity, not per public BOT."""

import threading

from core.agent.run_manager import AgentRunManager


def test_public_bot_allows_simultaneous_users_but_replaces_same_users_run():
    runs = AgentRunManager()
    release = threading.Event()
    entered = {}
    finished = {}

    class BlockingEngine:
        def run_agent_stream(self, _bot_id, message, **_kwargs):
            entered[message].set()
            try:
                assert release.wait(5)
                yield {"event": "done", "data": {"status": "success"}}
            finally:
                finished[message].set()

    def start(message, subject, *, tenant="tenant-a"):
        entered[message] = threading.Event()
        finished[message] = threading.Event()
        run = runs.start_run(
            "default_assistant", message, BlockingEngine(), None,
            user_id=subject, is_admin=False, is_operator=True,
            issuer="https://issuer-a", tenant_id=tenant,
        )
        assert entered[message].wait(3)
        return run

    try:
        alice = start("alice-first", "alice")
        bob = start("bob-first", "bob")
        other_tenant = start("alice-other-tenant", "alice", tenant="tenant-b")
        assert not alice.is_stopped and not bob.is_stopped and not other_tenant.is_stopped
        assert runs.get_active_run("default_assistant", "alice", "https://issuer-a", "tenant-a") is alice
        assert runs.get_active_run("default_assistant", "bob", "https://issuer-a", "tenant-a") is bob
        replacement = start("alice-second", "alice")
        assert alice.is_stopped
        assert not bob.is_stopped and not other_tenant.is_stopped and not replacement.is_stopped
        assert runs.get_active_run("default_assistant", "alice", "https://issuer-a", "tenant-a") is replacement
    finally:
        release.set()
        for done in finished.values():
            assert done.wait(3)
