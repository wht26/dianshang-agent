"""Anonymous local chat sessions, separate from v0.3 business data."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS qa_sessions (digest TEXT PRIMARY KEY, created REAL);
                CREATE TABLE IF NOT EXISTS qa_turns (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, question TEXT,
                    answer TEXT, sources TEXT, mode TEXT, created REAL);
            ''')
            if "product_id" not in {row[1] for row in db.execute("PRAGMA table_info(qa_turns)")}:
                db.execute("ALTER TABLE qa_turns ADD COLUMN product_id TEXT")

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, token):
        if token and len(token) <= 100:
            with self.db() as db:
                if db.execute("SELECT 1 FROM qa_sessions WHERE digest=?", (self.digest(token),)).fetchone():
                    return token
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO qa_sessions VALUES (?,?)", (self.digest(token), time.time()))
        return token

    def history(self, token, limit=20):
        with self.db() as db:
            rows = db.execute("SELECT question,answer,sources,mode,product_id FROM qa_turns WHERE session=? "
                              "ORDER BY id DESC LIMIT ?", (self.digest(token), limit)).fetchall()
        return [{"question": row["question"], "answer": row["answer"],
                 "sources": json.loads(row["sources"]), "mode": row["mode"],
                 "product_id": row["product_id"]} for row in reversed(rows)]

    def append(self, token, question, result):
        with self.db() as db:
            db.execute("INSERT INTO qa_turns(session,question,answer,sources,mode,created,product_id) VALUES (?,?,?,?,?,?,?)",
                       (self.digest(token), question, result["answer"], json.dumps(result["sources"], ensure_ascii=False),
                        result["mode"], time.time(), result.get("product_id")))
