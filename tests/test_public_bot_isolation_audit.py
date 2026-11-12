"""End-to-end resource boundaries for shared BOT definitions and personal state."""

from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from core.agent.bot_manager import BotManager
from core.agent.engine import AgentEngine
from core.agent.rag_manager import RagManager
from core.agent.tools import EnhancePromptTool, SaveSkillTool
from core.auth import CurrentUser
from core.runtime_context import (
    runtime_auth_scope, runtime_is_admin, runtime_is_operator, runtime_user_id,
)


def identity(subject, issuer='https://issuer-a', tenant='tenant-a', *, admin=False):
    return CurrentUser(subject, frozenset({'admin', 'operator'} if admin else {'operator'}),
                       claims={'iss': issuer, 'tenant_id': tenant})


@contextmanager
def acting_as(user):
    scope = runtime_auth_scope.set(user.auth_scope)
    subject = runtime_user_id.set(user.subject)
    admin = runtime_is_admin.set(user.is_admin)
    operator = runtime_is_operator.set(user.is_operator)
    try:
        yield
    finally:
        runtime_is_operator.reset(operator)
        runtime_is_admin.reset(admin)
        runtime_user_id.reset(subject)
        runtime_auth_scope.reset(scope)


@pytest.fixture
def resources(tmp_path, monkeypatch):
    import app as app_module
    import apis.agent as routes
    import core.auth as auth
    import core.agent.bot_manager as bot_module
    import core.agent.rag_manager as rag_module

    manager = BotManager(data_dir=str(tmp_path / 'data'))
    engine = AgentEngine(manager)
    rag = RagManager(data_root=manager.bots_dir)
    monkeypatch.setattr(bot_module, 'bot_manager', manager)
    monkeypatch.setattr(rag_module, 'rag_manager', rag)
    monkeypatch.setattr(routes, 'bot_manager', manager)
    monkeypatch.setattr(routes, 'agent_engine', engine)
    monkeypatch.setattr(routes, 'rag_manager', rag)
    state = SimpleNamespace(user=identity('alice'))
    monkeypatch.setattr(auth, 'authenticate_request', lambda _request: state.user)
    return manager, engine, rag, TestClient(app_module.app), state


def test_public_skill_source_and_deletion_are_user_partitioned(resources):
    manager, _, _, _, _ = resources
    alice, bob = identity('alice', admin=True), identity('bob', admin=True)
    with acting_as(alice):
        manager.save_skill('default_assistant', 'diagnose', 'alice private workflow', "result = 'alice secret'")
        alice_dir = manager._skills_dir('default_assistant')
    with acting_as(bob):
        manager.save_skill('default_assistant', 'diagnose', 'bob private workflow', "result = 'bob secret'")
        bob_dir = manager._skills_dir('default_assistant')
    with acting_as(alice):
        skill = manager.get_skill('default_assistant', 'diagnose')
        assert skill.code == "result = 'alice secret'"
        assert skill.integrity_status == 'verified'
    assert alice_dir != bob_dir
    with acting_as(bob):
        assert manager.delete_skill('default_assistant', 'diagnose')
    with acting_as(alice):
        assert manager.get_skill('default_assistant', 'diagnose').integrity_status == 'verified'


def test_signed_in_reader_does_not_import_quarantined_public_skills(resources):
    manager, _, _, client, _ = resources
    root = Path(manager._bot_dir('default_assistant')) / 'skills'
    root.mkdir(exist_ok=True)
    (root / 'old_private.json').write_text(json.dumps({'name': 'old_private', 'description': 'pre-auth private data'}))
    (root / 'old_private.py').write_text("result = 'PRE_AUTH_PRIVATE_SOURCE'")
    response = client.get('/api/agent/bots/default_assistant/skills')
    assert response.status_code == 200
    assert response.json()['skills'] == []
    assert client.get('/api/agent/bots/default_assistant/skills/old_private').status_code == 404
    assert (root / 'old_private.py').is_file()


def test_old_scoped_skill_row_cannot_launder_shared_root_source(resources):
    import hashlib

    manager, _, _, client, state = resources
    code = "result = 'QUARANTINED_ROOT_SOURCE'"
    root = Path(manager._bot_dir('default_assistant')) / 'skills'
    (root / 'legacy.py').write_text(code)
    with acting_as(state.user):
        manager.metadata_store.upsert_skill(
            bot_id='default_assistant', skill={'name': 'legacy', 'description': 'legacy metadata'},
            code_uri=f'local://{root}/legacy.py', code_sha256=hashlib.sha256(code.encode()).hexdigest(),
            **manager._metadata_scope('default_assistant'),
        )
    skill = client.get('/api/agent/bots/default_assistant/skills/legacy').json()['skill']
    assert skill['integrity_status'] == 'missing'
    assert skill['code'] == ''
    assert client.delete('/api/agent/bots/default_assistant/skills/legacy').json()['success']
    assert (root / 'legacy.py').read_text() == code


def test_public_skill_crud_is_personal_and_ordinary_users_can_save(resources):
    manager, _, _, client, state = resources
    response = client.post('/api/agent/bots/default_assistant/skills', json={
        'name': 'personal', 'description': 'alice only', 'python_code': "result = 'ALICE_ONLY'", 'input_schema': {},
    })
    assert response.status_code == 200
    for other in (identity('bob'), identity('alice', issuer='https://issuer-b'),
                  identity('alice', tenant='tenant-b'), identity('local', admin=True)):
        state.user = other
        assert client.get('/api/agent/bots/default_assistant/skills').json()['skills'] == []
        assert client.get('/api/agent/bots/default_assistant/skills/personal').status_code == 404
        assert client.delete('/api/agent/bots/default_assistant/skills/personal').json()['success'] is False
    state.user = identity('alice')
    assert client.get('/api/agent/bots/default_assistant/skills/personal').json()['skill']['code'] == "result = 'ALICE_ONLY'"
    assert client.delete('/api/agent/bots/default_assistant/skills/personal').json()['success']
    with acting_as(state.user):
        assert manager.list_skills('default_assistant') == []


def test_personal_skill_quota_and_metadata_limits_return_422_without_writing(resources, monkeypatch):
    import core.agent.bot_manager as module

    manager, _, _, client, state = resources
    monkeypatch.setattr(module, 'MAX_SKILLS_PER_SCOPE', 1)
    def save(name, **extra):
        return client.post('/api/agent/bots/default_assistant/skills', json={
            'name': name, 'description': 'personal', 'python_code': 'result = 1', **extra,
        })
    assert save('first').status_code == 200
    assert save('second').status_code == 422
    assert save('first', python_code='result = 2').status_code == 200
    assert save('first', input_schema={'oversized': 'x' * 32768}).status_code == 422
    state.user = identity('bob')
    assert save('second').status_code == 200
    with acting_as(identity('alice')):
        assert [item.name for item in manager.list_skills('default_assistant')] == ['first']
        assert manager.get_skill('default_assistant', 'first').code == 'result = 2'


def test_authenticated_subject_local_creates_private_bot_owned_by_local(resources):
    from core.agent.tools import CreateBotTool

    manager, _, _, client, state = resources
    state.user = identity('local')
    with acting_as(state.user):
        created = CreateBotTool(manager).forward('authenticated local private bot', 'synthetic goal')
    assert created['success']
    bot_id = created['bot']['id']
    bot = manager.get_bot(bot_id)
    assert bot.owner_id == 'local' and bot.issuer == state.user.issuer
    assert client.get(f'/api/agent/bots/{bot_id}').status_code == 200


def test_tenant_admin_cannot_edit_global_public_definitions_or_claim_legacy_state(resources):
    manager, _, _, client, state = resources
    state.user = identity('tenant-admin', admin=True)
    for method, url, body in (
        ('put', '/api/agent/bots/css_live_operator', {'base_prompt': 'tenant A controls all tenants'}),
        ('post', '/api/agent/bots/default_assistant/guidelines', {'instruction': 'global injection', 'reason': 'test'}),
        ('post', '/api/agent/bots/default_assistant/suggested-prompts', {}),
        ('post', '/api/agent/bots/default_assistant/legacy-state/migrate', {'target_user_id': 'tenant-admin'}),
    ):
        assert getattr(client, method)(url, json=body).status_code == 403, url
    assert client.delete('/api/agent/bots/css_live_operator').status_code == 403
    with acting_as(state.user):
        bot = manager.create_bot('tenant-shared', 'tenant BOT', visibility='public')
    assert client.post(f'/api/agent/bots/{bot.id}/guidelines', json={'instruction': 'tenant shared rule'}).status_code == 200
    assert client.put(f'/api/agent/bots/{bot.id}', json={'description': 'tenant admin managed'}).status_code == 200
    state.user = identity('ordinary-reader')
    assert client.post(f'/api/agent/bots/{bot.id}/suggested-prompts', json={}).status_code == 403


@pytest.mark.parametrize('admin', [False, True])
def test_public_auto_enhancement_keeps_conversation_preferences_personal(resources, admin):
    manager, engine, _, client, state = resources
    alice = identity('alice', admin=admin)
    with acting_as(alice):
        result = EnhancePromptTool('default_assistant', manager).forward('ALICE_PRIVATE_PREFERENCE', 'private context')
        assert '❌' not in result
        assert 'ALICE_PRIVATE_PREFERENCE' in engine.assemble_system_prompt(manager.get_bot('default_assistant'))
    state.user = identity('bob')
    assert 'ALICE_PRIVATE_PREFERENCE' not in client.get('/api/agent/bots/default_assistant').text
    assert manager.get_bot('default_assistant').learned_guidelines == []


def test_prompt_preview_uses_same_user_scope_for_all_memory_layers(resources):
    manager, engine, rag, client, state = resources
    alice = state.user
    with acting_as(alice):
        manager.save_memory('default_assistant', 'identity', 'ALICE_MEMORY_SENTINEL')
        for n in range(28):
            manager.append_history('default_assistant', 'user', f'ALICE_HISTORY_SENTINEL {n}')
        engine._remember_sandbox(engine._sandbox_key('default_assistant'), {'private_value': 'ALICE_SANDBOX_SENTINEL'})
        rag.add_document('default_assistant', 'ALICE_DOCUMENT_SENTINEL.txt', b'ALICE_KNOWLEDGE_SENTINEL runbook')
    response = client.get('/api/agent/bots/default_assistant')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'Authorization' in response.headers['vary']
    assert 'ALICE_MEMORY_SENTINEL' in response.text
    assert 'ALICE_HISTORY_SENTINEL' in response.text
    assert 'ALICE_SANDBOX_SENTINEL' in response.text
    assert 'ALICE_DOCUMENT_SENTINEL' in response.text
    for other in (identity('bob'), identity('alice', issuer='https://issuer-b'),
                  identity('alice', tenant='tenant-b'), identity('local', admin=True)):
        state.user = other
        response = client.get('/api/agent/bots/default_assistant')
        assert response.status_code == 200
        assert 'ALICE_' not in response.text
        assert client.get('/api/agent/bots/default_assistant/history').json()['history'] == []
        assert client.get('/api/agent/bots/default_assistant/export?format=markdown').status_code == 400


def test_public_orchestration_resource_is_personal_even_for_admin(resources):
    manager, _, _, client, state = resources
    with acting_as(state.user):
        manager.record_orchestration({
            'id': 'alice-event', 'caller_bot_id': 'default_assistant', 'target_bot_id': 'css_live_operator',
            'task': 'ALICE_PRIVATE_ORCHESTRATION_TASK', 'status': 'success',
        })
    assert 'ALICE_PRIVATE_ORCHESTRATION_TASK' in client.get('/api/agent/bots/default_assistant/orchestrations').text
    state.user = identity('bob', admin=True)
    assert client.get('/api/agent/bots/default_assistant/orchestrations').json()['orchestrations'] == []
    # Explicit management topology remains a tenant-administration capability.
    assert 'ALICE_PRIVATE_ORCHESTRATION_TASK' in client.get('/api/agent/topology').text


def test_concurrent_background_workers_keep_prompt_history_rag_and_approval_identity(resources):
    import threading
    from core.agent.run_manager import AgentRunManager

    manager, engine, rag, _, _ = resources
    alice, bob = identity('alice'), identity('bob')
    for user in (alice, bob):
        with acting_as(user):
            manager.save_memory('default_assistant', 'personal', f'{user.subject.upper()}_WORKER_SECRET')
            rag.add_document('default_assistant', f'{user.subject}-runbook.txt', f'{user.subject.upper()}_WORKER_SECRET'.encode())
    barrier = threading.Barrier(2)
    observed = {}
    complete = {subject: threading.Event() for subject in ('alice', 'bob')}

    class ProbeEngine:
        def run_agent_stream(self, bot_id, user_message, **_kwargs):
            subject = runtime_user_id.get()
            try:
                barrier.wait(timeout=5)
                manager.append_history(bot_id, 'user', f'{subject.upper()}_WORKER_HISTORY')
                observed[subject] = (
                    engine.assemble_system_prompt(manager.get_bot(bot_id)),
                    manager.get_history(bot_id), rag.search(bot_id, f'{subject.upper()}_WORKER_SECRET'),
                    runtime_auth_scope.get(), runtime_is_admin.get(), runtime_is_operator.get(),
                )
                yield {'event': 'done', 'data': {'status': 'success'}}
            finally:
                complete[subject].set()

    runs = AgentRunManager()
    active = []
    for user in (alice, bob):
        active.append(runs.start_run(
            'default_assistant', 'synthetic concurrent task', ProbeEngine(), manager,
            user_id=user.subject, is_admin=user.is_admin, is_operator=user.is_operator,
            issuer=user.issuer, tenant_id=user.tenant_id,
        ))
    assert complete['alice'].wait(10) and complete['bob'].wait(10)
    for user, other in ((alice, bob), (bob, alice)):
        prompt, history, chunks, scope, admin, operator = observed[user.subject]
        assert f'{user.subject.upper()}_WORKER_SECRET' in prompt
        assert f'{other.subject.upper()}_WORKER_SECRET' not in prompt
        assert all(other.subject.upper() not in item['content'] for item in history)
        assert chunks and all(other.subject.upper() not in item['content'] for item in chunks)
        assert scope == user.auth_scope and not admin and operator
    # Pending approvals are resolved through the complete identity key.
    from core.agent.run_manager import AgentRun
    pending = AgentRun('synthetic-run', 'default_assistant', 'task', user_id='alice', issuer=alice.issuer, tenant_id=alice.tenant_id)
    pending.resolve_approval = lambda _approval, _decision: True
    runs.runs[pending.run_id] = pending
    runs.active_bot_runs[runs._run_key('default_assistant', 'alice', alice.issuer, alice.tenant_id)] = pending.run_id
    assert not runs.resolve_approval('default_assistant', 'approval-a', 'approve', 'bob', bob.issuer, bob.tenant_id)
    assert not runs.resolve_approval('default_assistant', 'approval-a', 'approve', 'alice', alice.issuer, 'other-tenant')
    assert runs.resolve_approval('default_assistant', 'approval-a', 'approve', 'alice', alice.issuer, alice.tenant_id)


def test_skill_failed_sql_commit_preserves_previous_code_and_cleans_new_source(resources, monkeypatch):
    manager, _, _, _, state = resources
    with acting_as(state.user):
        manager.save_skill('default_assistant', 'workflow', 'old', "result = 'OLD'")
        directory = Path(manager._skills_dir('default_assistant'))
        previous_files = set(directory.iterdir())
        def fail(**_kwargs):
            raise RuntimeError('injected SQL failure')
        monkeypatch.setattr(manager.metadata_store, 'upsert_skill', fail)
        with pytest.raises(RuntimeError, match='SQL failure'):
            manager.save_skill('default_assistant', 'workflow', 'new', "result = 'NEW'")
        assert set(directory.iterdir()) == previous_files
        assert manager.get_skill('default_assistant', 'workflow').code == "result = 'OLD'"


def _save_skill_in_process(data_dir, subject):
    manager = BotManager(data_dir=data_dir)
    with acting_as(identity(subject)):
        manager.save_skill('default_assistant', 'shared_name', f'{subject} workflow', f"result = '{subject}'\r\n")
        for _ in range(10):
            manager.record_run_stats('default_assistant', {'total_tokens': 1})
        loaded = manager.get_skill('default_assistant', 'shared_name')
        return loaded.code, loaded.integrity_status, manager._skills_dir('default_assistant')


def test_two_processes_save_same_skill_name_without_crossing_source(resources):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    manager, _, _, _, _ = resources
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn')) as pool:
        alice = pool.submit(_save_skill_in_process, manager.data_dir, 'alice')
        bob = pool.submit(_save_skill_in_process, manager.data_dir, 'bob')
        alice_result, bob_result = alice.result(timeout=20), bob.result(timeout=20)
    assert alice_result[:2] == ("result = 'alice'\r\n", 'verified')
    assert bob_result[:2] == ("result = 'bob'\r\n", 'verified')
    assert alice_result[2] != bob_result[2]
    assert manager.get_bot('default_assistant').stats.total_runs == 20
    assert manager.get_bot('default_assistant').stats.total_tokens == 20


def test_shared_bot_stats_update_does_not_overwrite_other_worker_or_definition(resources, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    manager, _, _, _, _ = resources
    other_manager = BotManager(data_dir=manager.data_dir)
    barrier = threading.Barrier(2)
    for current in (manager, other_manager):
        original = current.get_bot

        def read_before_concurrent_write(bot_id, get=original):
            bot = get(bot_id)
            barrier.wait(timeout=5)
            return bot

        monkeypatch.setattr(current, 'get_bot', read_before_concurrent_write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(current.record_run_stats, 'default_assistant', {'total_tokens': 10})
                for current in (manager, other_manager)]
        for job in jobs:
            job.result(timeout=10)
    definition = manager.metadata_store.get_bot('default_assistant')
    assert definition['stats']['total_runs'] == 2
    assert definition['stats']['total_tokens'] == 20


def test_turn_completion_summary_stays_in_canonical_scoped_database(resources, monkeypatch):
    from core.agent import engine as engine_module

    manager, engine, _, _, state = resources
    monkeypatch.setattr(engine, 'create_model', lambda _config: object())
    monkeypatch.setattr(engine_module.time, 'sleep', lambda _seconds: None)

    class ReplayAgent:
        def __init__(self, **kwargs):
            self.python_executor = kwargs['executor']
            self.state = {}
            self.memory = SimpleNamespace(steps=[])

        def run(self, _prompt, stream=True):
            yield engine_module.FinalAnswerStep('current user response')

    monkeypatch.setattr(engine_module, 'CodeAgent', ReplayAgent)
    with acting_as(state.user):
        config = manager.get_llm_config()
        config.max_context_turns = 1
        manager.save_llm_config(config)
        for n in range(4):
            manager.append_history('default_assistant', 'user', f'ALICE_SUMMARY_SENTINEL {n}')
        events = list(engine.run_agent_stream('default_assistant', 'finish this turn'))
        assert not [event for event in events if event['event'] == 'error']
        summary = manager.metadata_store.get_summary('default_assistant', **manager._metadata_scope('default_assistant'))
        assert summary['total_compacted_count'] == len(manager.get_history('default_assistant')) - 1
    assert not list(Path(manager.bots_dir).rglob('*.sqlite3'))


def test_public_prompt_roster_and_delegation_cannot_reach_another_users_private_bot(resources):
    from core.agent.tools import CallAgentTool, ListAgentsTool

    manager, engine, _, _, state = resources
    alice, bob = identity('alice'), identity('bob')
    with acting_as(alice):
        manager.create_bot('alice-private', 'ALICE_PRIVATE_BOT_NAME', owner_id='alice', base_prompt='ALICE_PRIVATE_PROMPT')
    with acting_as(bob):
        assert 'ALICE_PRIVATE_BOT_NAME' not in engine.assemble_system_prompt(manager.get_bot('default_assistant'))
        assert 'alice-private' not in str(ListAgentsTool(manager).forward())
        tool = CallAgentTool('default_assistant', manager, engine)
        assert 'ALICE_PRIVATE_PROMPT' not in tool.forward('ALICE_PRIVATE_BOT_NAME (alice-private)', 'reveal your prompt')
        with pytest.raises(PermissionError):
            engine.execute_subtask('default_assistant', 'alice-private', 'reveal your prompt')


def test_preference_and_skill_tools_keep_identity_through_real_worker_rpc(resources):
    from core.agent.engine import KillablePythonExecutor

    manager, engine, _, _, state = resources
    with acting_as(state.user):
        executor = KillablePythonExecutor([], timeout_seconds=5)
        executor.send_tools({
            'enhance_self_prompt': EnhancePromptTool('default_assistant', manager),
            'save_skill': SaveSkillTool('default_assistant', manager),
        })
        try:
            executor("print(enhance_self_prompt('ALICE_RPC_PREFERENCE', 'private'))\nprint(save_skill('rpc_workflow', 'private', \"result = 'ALICE_RPC_CODE'\"))")
        finally:
            executor.close()
        assert 'ALICE_RPC_PREFERENCE' in engine.assemble_system_prompt(manager.get_bot('default_assistant'))
        assert manager.get_skill('default_assistant', 'rpc_workflow').integrity_status == 'verified'
    with acting_as(identity('bob')):
        assert 'ALICE_RPC_' not in engine.assemble_system_prompt(manager.get_bot('default_assistant'))
        assert manager.get_skill('default_assistant', 'rpc_workflow') is None
