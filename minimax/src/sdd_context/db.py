"""seekdb/OceanBase persistence for engineering materials.

The database stores the spec, source and test documents.  Context selection is
performed with a real SQL query against that table; the returned rows are what
the context builder places in the prompt sent to MiniMax.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

import pymysql
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = ROOT / "sample_app"

DB_CONFIG = {
    "host": os.getenv("SEEKDB_HOST", "127.0.0.1"),
    "port": int(os.getenv("SEEKDB_PORT", "2898")),
    "user": os.getenv("SEEKDB_USER", "root"),
    "password": os.getenv("SEEKDB_PASSWORD", ""),
    "database": os.getenv("SEEKDB_DATABASE", "minimax_sdd"),
    "charset": "utf8mb4",
    "cursorclass": DictCursor,
    "autocommit": True,
}

MATERIALS = (
    ("spec", SAMPLE / "specs" / "pricing.md"),
    ("code", SAMPLE / "src" / "pricing.py"),
    ("test", SAMPLE / "tests" / "test_pricing.py"),
)


def connect(with_db: bool = True):
    cfg = dict(DB_CONFIG)
    if not with_db:
        cfg.pop("database")
    return pymysql.connect(**cfg)


def init_db(reset: bool = False) -> list[tuple[str, str]]:
    """Create the schema and import materials; return (kind, path) pairs."""
    cfg = dict(DB_CONFIG)
    dbname = cfg.pop("database")
    server = pymysql.connect(**cfg)
    try:
        with server.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{dbname}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
            )
    finally:
        server.close()

    conn = connect()
    try:
        with conn.cursor() as cur:
            if reset:
                cur.execute("DROP TABLE IF EXISTS sdd_documents")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sdd_documents (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    kind VARCHAR(16) NOT NULL,
                    path VARCHAR(255) NOT NULL,
                    content LONGTEXT NOT NULL,
                    imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE KEY uk_path (path)
                ) CHARACTER SET utf8mb4
                """
            )
            # seekdb supports full-text indexes; keep a graceful fallback so the
            # demo also initializes on a plain MySQL-compatible endpoint.
            try:
                cur.execute(
                    "CREATE FULLTEXT INDEX ft_sdd_documents_content "
                    "ON sdd_documents(content)"
                )
            except Exception:
                pass
            imported = []
            for kind, path in MATERIALS:
                content = path.read_text(encoding="utf-8")
                rel = path.relative_to(ROOT).as_posix()
                cur.execute(
                    """
                    INSERT INTO sdd_documents (kind, path, content)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE kind=VALUES(kind), content=VALUES(content)
                    """,
                    (kind, rel, content),
                )
                imported.append((kind, rel))
            return imported
    finally:
        conn.close()


def query_materials(task: str, limit: int = 20) -> list[dict]:
    """Select relevant engineering documents with SQL.

    The primary path uses seekdb full-text search.  A LIKE fallback keeps the
    module runnable on a database where the parser rejects a fulltext query.
    """
    conn = connect()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    SELECT kind, path, content
                    FROM sdd_documents
                    WHERE MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE)
                    ORDER BY kind DESC, path
                    LIMIT %s
                    """,
                    (task, limit),
                )
                rows = cur.fetchall()
            except Exception:
                like = f"%{task.split()[0] if task.split() else task}%"
                cur.execute(
                    """
                    SELECT kind, path, content
                    FROM sdd_documents
                    WHERE content LIKE %s OR path LIKE %s
                    ORDER BY FIELD(kind, 'spec', 'code', 'test'), path
                    LIMIT %s
                    """,
                    (like, like, limit),
                )
                rows = cur.fetchall()
            # Always include the three material kinds that the task requires,
            # while keeping the selected rows and their real paths as citations.
            if len(rows) < 3:
                cur.execute(
                    "SELECT kind, path, content FROM sdd_documents "
                    "ORDER BY FIELD(kind, 'spec', 'code', 'test'), path"
                )
                rows = cur.fetchall()
            return list(rows)
    finally:
        conn.close()


def import_path(path: Path, kind: str | None = None) -> None:
    """Re-import a changed document to demonstrate spec updates are observed."""
    conn = connect()
    try:
        rel = path.relative_to(ROOT).as_posix()
        kind = kind or ("spec" if "specs" in path.parts else "test" if "tests" in path.parts else "code")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sdd_documents (kind, path, content) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE kind=VALUES(kind), content=VALUES(content)",
                (kind, rel, path.read_text(encoding="utf-8")),
            )
    finally:
        conn.close()
