"""One-way, read-only SQLite snapshot import into an empty PostgreSQL database."""

import argparse
import os
import sqlite3
from pathlib import Path

from psycopg import sql

from .commerce import Commerce
from .knowledge import Knowledge
from .memory import Memory
from .store import Store

TABLES = ('qa_sessions', 'qa_turns', 'customer_memory', 'shop_settings', 'kb_documents',
          'commerce_stock', 'commerce_orders', 'commerce_refunds', 'commerce_shipment_events', 'commerce_audit')
IDENTITIES = ('qa_turns', 'commerce_shipment_events', 'commerce_audit')


def migrate(source, dsn=None):
    source = Path(source).resolve(strict=True)
    store = Store(dsn)
    Commerce(store)
    Memory(store)
    Knowledge(db_path=store.dsn)
    store.initialize('CREATE TABLE IF NOT EXISTS shop_settings (id INTEGER PRIMARY KEY, payload TEXT)')
    snapshot = sqlite3.connect(':memory:')
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
        original.backup(snapshot)
    counts = {}
    try:
        with store.db() as db:
            db.execute('SELECT pg_advisory_xact_lock(79214002)')
            for table in TABLES:
                if db.execute(sql.SQL('SELECT 1 FROM {} LIMIT 1').format(sql.Identifier(table))).fetchone():
                    raise ValueError('目标业务表必须为空；迁移已停止，未覆盖任何记录。')
            existing = {row[0] for row in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in TABLES:
                if table not in existing:
                    continue
                columns = [row[1] for row in snapshot.execute(f'PRAGMA table_info({table})')]
                query = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                    sql.Identifier(table), sql.SQL(',').join(map(sql.Identifier, columns)),
                    sql.SQL(',').join(sql.Placeholder() for _ in columns))
                counts[table] = 0
                for row in snapshot.execute(f'SELECT * FROM {table}'):
                    db.execute(query, row)
                    counts[table] += 1
            for table in IDENTITIES:
                db.execute(sql.SQL("SELECT setval(pg_get_serial_sequence(%s,'id'),COALESCE(MAX(id),1),MAX(id) IS NOT NULL) FROM {}").format(
                    sql.Identifier(table)), (table,))
    finally:
        snapshot.close()
    return counts


def main():
    parser = argparse.ArgumentParser(description='停止应用后，把旧SQLite快照导入空PostgreSQL；源文件不修改。')
    parser.add_argument('source')
    args = parser.parse_args()
    counts = migrate(args.source, os.getenv('DATABASE_URL'))
    print('迁移完成，仅报告记录数：', counts)


if __name__ == '__main__':
    main()
