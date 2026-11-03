"""PostgreSQL connections; no SQLite fallback in the application runtime."""

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


class Database:
    def __init__(self, dsn=None):
        self.dsn = str(dsn or os.getenv('DATABASE_URL', ''))
        if not self.dsn.startswith(('postgresql://', 'postgres://')):
            raise ValueError('请设置PostgreSQL DATABASE_URL；旧SQLite文件请使用迁移工具导入。')

    @contextmanager
    def db(self):
        # Connection context commits on success, rolls back on exceptions, and closes.
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as connection:
            yield connection

    def initialize(self, script):
        with self.db() as db:
            # Serialize schema creation across starting workers.
            db.execute('SELECT pg_advisory_xact_lock(79214001)')
            for statement in script.split(';'):
                if statement.strip():
                    db.execute(statement)
