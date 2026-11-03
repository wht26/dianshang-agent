import asyncio
import hashlib
import sqlite3

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError

from wang.coordination import RedisControl
from wang.migrate_sqlite import migrate
from wang.store import Store


def test_postgres_persists_between_connections(tmp_path):
    store = Store(tmp_path / 'persistent')
    token = store.session(None)
    store.append(token, '尺码', {'answer': '请提供尺码表', 'sources': [], 'mode': 'demo'})
    reopened = Store(store.dsn)
    assert reopened.history(token)[0]['question'] == '尺码'
    with reopened.db() as db:
        assert db.execute('SELECT version() AS version').fetchone()['version'].startswith('PostgreSQL')
        assert db.execute('SELECT created FROM qa_turns').fetchone()['created'] > 1_000_000_000


def test_redis_lease_shared_and_token_owned():
    async def run():
        first, second = RedisControl(), RedisControl()
        try:
            async with first.hold() as lease:
                assert await second.busy()
                with pytest.raises(HTTPException) as conflict:
                    async with second.hold():
                        pytest.fail('Concurrent lease acquired')
                assert conflict.value.status_code == 429
                await lease.ensure_owned()
                second.client.set(first.key, 'replacement', ex=30)
                with pytest.raises(HTTPException) as lost:
                    await lease.ensure_owned()
                assert lost.value.status_code == 503
            assert second.client.get(first.key) == 'replacement'
        finally:
            first.close()
            second.close()
    asyncio.run(run())


def test_redis_rate_limit_shared(monkeypatch):
    monkeypatch.setenv('WANG_CHAT_RATE_LIMIT', '2')
    async def run():
        first, second = RedisControl(), RedisControl()
        try:
            await first.allow('buyer')
            await second.allow('buyer')
            with pytest.raises(HTTPException) as limited:
                await first.allow('buyer')
            assert limited.value.status_code == 429
            assert first.client.ttl(first.prefix + ':rate:buyer') > 0
        finally:
            first.close()
            second.close()
    asyncio.run(run())


def test_redis_failure_does_not_fall_back(monkeypatch):
    control = RedisControl()
    def unavailable(*args, **kwargs):
        raise ConnectionError('private connection details')
    monkeypatch.setattr(control.client, 'exists', unavailable)
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(control.busy())
        assert error.value.status_code == 503
        assert 'private' not in error.value.detail
    finally:
        control.close()


def test_sqlite_migration_preserves_source_and_sequence(tmp_path):
    source = tmp_path / 'old.db'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE qa_sessions(digest TEXT PRIMARY KEY, created REAL); '
                         'CREATE TABLE qa_turns(id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, '
                         'question TEXT, answer TEXT, sources TEXT, mode TEXT, created REAL, product_id TEXT);')
        db.execute('INSERT INTO qa_sessions VALUES (?,?)', ('digest', 1700000000.75))
        db.execute('INSERT INTO qa_turns VALUES (?,?,?,?,?,?,?,?)',
                   (17, 'digest', 'question', 'answer', '[]', 'demo', 1700000000.75, None))
    before = hashlib.sha256(source.read_bytes()).digest()
    target = Store(tmp_path / 'migration-target')
    assert migrate(source, target.dsn)['qa_turns'] == 1
    assert hashlib.sha256(source.read_bytes()).digest() == before
    with target.db() as db:
        assert db.execute('SELECT created FROM qa_turns').fetchone()['created'] == 1700000000.75
        row = db.execute("INSERT INTO qa_turns(session) VALUES ('new') RETURNING id").fetchone()
        assert row['id'] == 18
    with pytest.raises(ValueError, match='必须为空'):
        migrate(source, target.dsn)
    with target.db() as db:
        assert db.execute('SELECT COUNT(*) FROM qa_turns').fetchone()['count'] == 2


def test_bad_rate_configuration_is_unavailable(monkeypatch):
    monkeypatch.setenv('WANG_CHAT_RATE_LIMIT', 'invalid')
    control = RedisControl()
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(control.allow('buyer'))
        assert error.value.status_code == 503
    finally:
        control.close()


def test_cross_instance_knowledge_refresh_and_health(tmp_path):
    from fastapi.testclient import TestClient
    from wang.app import create_app
    path = tmp_path / 'shared-store'
    with TestClient(create_app(path)) as first, TestClient(create_app(path)) as second:
        response = first.post('/api/knowledge/import', headers={'X-Wang-Client': 'web'},
                              json={'title': 'Shared document', 'content': 'Shared knowledge content'})
        assert response.status_code == 200
        document_id = response.json()['id']
        assert any(doc['id'] == document_id for doc in second.get('/api/knowledge').json()['documents'])
        health = second.get('/health')
        assert health.status_code == 200
        assert health.json()['database'] == 'postgresql'
        assert health.json()['coordination'] == 'redis'


def test_migration_rolls_back_all_inserted_rows(tmp_path):
    import psycopg
    source = tmp_path / 'broken.db'
    with sqlite3.connect(source) as db:
        db.executescript('CREATE TABLE qa_sessions(digest TEXT PRIMARY KEY, created REAL); '
                         'CREATE TABLE qa_turns(unknown_column TEXT);')
        db.execute('INSERT INTO qa_sessions VALUES (?,?)', ('must-rollback', 1700000000))
        db.execute("INSERT INTO qa_turns VALUES ('invalid schema')")
    target = Store(tmp_path / 'rollback-target')
    with pytest.raises(psycopg.errors.UndefinedColumn):
        migrate(source, target.dsn)
    with target.db() as db:
        assert db.execute('SELECT COUNT(*) FROM qa_sessions').fetchone()['count'] == 0
