"""Run all persistence tests on isolated real PostgreSQL schemas and Redis keys.

Legacy tmp_path arguments are logical test namespace names, never SQLite files.
"""

import os
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql
from redis import Redis

from wang.database import Database


@pytest.fixture(autouse=True)
def isolated_services(monkeypatch):
    base = os.getenv('TEST_DATABASE_URL')
    redis_url = os.getenv('TEST_REDIS_URL')
    if not base or not redis_url:
        pytest.fail('测试需要独立的TEST_DATABASE_URL和TEST_REDIS_URL；参见docs/STORAGE.md。')
    original_init = Database.__init__
    namespaces = {}
    prefix = 'wang_test_' + uuid.uuid4().hex

    def resolve(value):
        key = str(value or 'default')
        if key.startswith(('postgresql://', 'postgres://')):
            return key
        if key not in namespaces:
            schema = prefix + '_' + str(len(namespaces))
            with psycopg.connect(base, autocommit=True) as db:
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            parts = urlsplit(base)
            query = dict(parse_qsl(parts.query))
            query['options'] = '-c search_path=' + schema
            namespaces[key] = (schema, urlunsplit(parts._replace(query=urlencode(query, quote_via=quote))))
        return namespaces[key][1]

    def initialize(instance, dsn=None):
        original_init(instance, resolve(dsn))

    default_dsn = resolve('default')
    monkeypatch.setattr(Database, '__init__', initialize)
    monkeypatch.setenv('DATABASE_URL', default_dsn)
    monkeypatch.setenv('REDIS_URL', redis_url)
    monkeypatch.setenv('WANG_REDIS_PREFIX', prefix)
    monkeypatch.setenv('WANG_CHAT_RATE_LIMIT', '1000')
    monkeypatch.setenv('WANG_SHOP_FILE', str(Path(__file__).parents[1] / 'wang/shops/demo.json'))
    try:
        yield
    finally:
        with psycopg.connect(base, autocommit=True) as db:
            for schema, _ in namespaces.values():
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        client = Redis.from_url(redis_url)
        keys = list(client.scan_iter(match=prefix + ':*'))
        if keys:
            client.delete(*keys)
        client.close()
