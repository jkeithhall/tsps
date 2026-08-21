"""SQLite-backed store for committed file content and advisory file locks."""

import os
import sqlite3
import time


class _Store:
    """Authoritative committed file content + advisory file locks.

    Backed by SQLite so multiple MAGIS member processes share one source of
    truth. A Redis backend could implement the same surface; SQLite avoids
    requiring a running server.
    """

    def __init__(self, db_path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # isolation_level=None -> autocommit; we drive BEGIN IMMEDIATE manually
        # for cross-process critical sections.
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS committed_file (
                repo_key TEXT NOT NULL,
                file_path TEXT NOT NULL,
                content TEXT,
                last_committer TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                updated_at REAL,
                PRIMARY KEY (repo_key, file_path)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_lock (
                repo_key TEXT NOT NULL,
                file_path TEXT NOT NULL,
                holder TEXT,
                acquired_at REAL,
                PRIMARY KEY (repo_key, file_path)
            )
            """
        )

    # --- committed files ---
    def get_file(self, repo_key: str, file_path: str):
        return self.conn.execute(
            "SELECT content, last_committer, version FROM committed_file WHERE repo_key=? AND file_path=?",
            (repo_key, file_path),
        ).fetchone()

    def upsert_file(self, repo_key, file_path, content, committer, version):
        self.conn.execute(
            """
            INSERT INTO committed_file (repo_key, file_path, content, last_committer, version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_key, file_path) DO UPDATE SET
                content=excluded.content,
                last_committer=excluded.last_committer,
                version=excluded.version,
                updated_at=excluded.updated_at
            """,
            (repo_key, file_path, content, committer, version, time.time()),
        )

    # --- locks ---
    def get_lock(self, repo_key, file_path):
        return self.conn.execute(
            "SELECT holder, acquired_at FROM file_lock WHERE repo_key=? AND file_path=?",
            (repo_key, file_path),
        ).fetchone()

    def set_lock(self, repo_key, file_path, holder):
        self.conn.execute(
            """
            INSERT INTO file_lock (repo_key, file_path, holder, acquired_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(repo_key, file_path) DO UPDATE SET
                holder=excluded.holder, acquired_at=excluded.acquired_at
            """,
            (repo_key, file_path, holder, time.time()),
        )

    def del_lock(self, repo_key, file_path):
        self.conn.execute(
            "DELETE FROM file_lock WHERE repo_key=? AND file_path=?", (repo_key, file_path)
        )

    def list_files(self, repo_key):
        return self.conn.execute(
            "SELECT file_path, content, last_committer, version FROM committed_file WHERE repo_key=? ORDER BY file_path",
            (repo_key,),
        ).fetchall()

    def clear_repo(self, repo_key):
        self.conn.execute("DELETE FROM committed_file WHERE repo_key=?", (repo_key,))
        self.conn.execute("DELETE FROM file_lock WHERE repo_key=?", (repo_key,))

    def begin_immediate(self):
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        self.conn.execute("COMMIT")

    def rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    def close(self):
        self.conn.close()
