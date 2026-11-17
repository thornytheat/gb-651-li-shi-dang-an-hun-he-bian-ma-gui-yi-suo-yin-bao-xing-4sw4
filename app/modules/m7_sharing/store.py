"""馆际共享的持久化层。

所有写操作用 ``BEGIN IMMEDIATE`` 串行化，授权变更、交付、下载凭证核验与审计
落在同一个事务里。这样“权限变更”和“下载”只有一个明确的先后边界，不会出现
读到半新半旧状态的情况。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ...manifest import canonical_digest
from ...timeutil import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    library_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    scope        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    updated_by   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grant_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    library_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    version     INTEGER NOT NULL,
    scope       TEXT NOT NULL,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    note        TEXT,
    effective_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id              TEXT PRIMARY KEY,
    library_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    grant_version   INTEGER NOT NULL,
    grant_scope     TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempt         INTEGER NOT NULL DEFAULT 0,
    progress        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    requested_by    TEXT NOT NULL,
    manifest        TEXT,
    manifest_digest TEXT,
    items           INTEGER NOT NULL DEFAULT 0,
    reason          TEXT,
    last_error      TEXT,
    UNIQUE(library_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS delivery_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    library_id  TEXT NOT NULL,
    items       INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id            TEXT PRIMARY KEY,
    delivery_id   TEXT NOT NULL,
    library_id    TEXT NOT NULL,
    grant_version INTEGER NOT NULL,
    issued_at     TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    status        TEXT NOT NULL,
    used_at       TEXT,
    closed_at     TEXT,
    closed_reason TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    library_id  TEXT NOT NULL,
    at          TEXT NOT NULL,
    items       INTEGER NOT NULL,
    manifest_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    action      TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    library_id  TEXT,
    grant_id    INTEGER,
    delivery_id TEXT,
    ticket_id   TEXT,
    detail      TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_library ON audit(library_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_library ON deliveries(library_id);
CREATE INDEX IF NOT EXISTS idx_tickets_delivery ON tickets(delivery_id);
"""

AUDIT_FIELDS = ("at", "actor", "actor_role", "action", "outcome", "library_id",
                "grant_id", "delivery_id", "ticket_id", "detail")


def audit_hash(prev_hash: str, row: dict) -> str:
    material = json.dumps(
        {key: row.get(key) for key in AUDIT_FIELDS},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256((prev_hash + "|" + material).encode("utf-8")).hexdigest()


_STORES: dict[str, "SharingStore"] = {}


def store_for(path: str) -> "SharingStore":
    """每个数据库路径复用一个存储对象，避免每请求重复建表取写锁。"""
    store = _STORES.get(path)
    if store is None:
        store = SharingStore(path)
        _STORES[path] = store
    return store


class SharingStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @contextmanager
    def read(self):
        with self._lock:
            db = self.connect()
            try:
                yield db
            finally:
                db.close()

    @contextmanager
    def write(self):
        """串行写事务：进入即取写锁，退出时提交；异常回滚。"""
        with self._lock:
            db = self.connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            finally:
                db.close()

    # ---- audit -------------------------------------------------------
    @staticmethod
    def append_audit(db, *, actor: str, actor_role: str, action: str, outcome: str,
                     library_id: str | None = None, grant_id: int | None = None,
                     delivery_id: str | None = None, ticket_id: str | None = None,
                     detail: dict | None = None) -> dict:
        row = {
            "at": iso(utcnow()),
            "actor": actor,
            "actor_role": actor_role,
            "action": action,
            "outcome": outcome,
            "library_id": library_id,
            "grant_id": grant_id,
            "delivery_id": delivery_id,
            "ticket_id": ticket_id,
            "detail": json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
        }
        prev = db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = prev["hash"] if prev else "GENESIS"
        row["prev_hash"] = prev_hash
        row["hash"] = audit_hash(prev_hash, row)
        cur = db.execute(
            "INSERT INTO audit (at, actor, actor_role, action, outcome, library_id, grant_id,"
            " delivery_id, ticket_id, detail, prev_hash, hash)"
            " VALUES (:at, :actor, :actor_role, :action, :outcome, :library_id, :grant_id,"
            " :delivery_id, :ticket_id, :detail, :prev_hash, :hash)",
            row,
        )
        row["id"] = cur.lastrowid
        return row

    # ---- grants ------------------------------------------------------
    @staticmethod
    def grant_row(db, library_id: str) -> dict | None:
        row = db.execute("SELECT * FROM grants WHERE library_id=?", (library_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def set_grant(db, *, library_id: str, scope: list[str], actor: str,
                  actor_role: str, note: str | None) -> tuple[dict, dict]:
        now = iso(utcnow())
        existing = SharingStore.grant_row(db, library_id)
        scope_json = json.dumps(sorted(scope), ensure_ascii=False)
        if existing and existing["status"] == "active" and existing["scope"] == scope_json:
            return existing, {"action": "GRANT_UNCHANGED", "version": existing["version"],
                              "effective_at": existing["updated_at"], "changed": False}
        version = (existing["version"] + 1) if existing else 1
        if existing:
            db.execute(
                "UPDATE grants SET status='active', version=?, scope=?, updated_at=?, updated_by=?"
                " WHERE library_id=?",
                (version, scope_json, now, actor, library_id),
            )
        else:
            db.execute(
                "INSERT INTO grants (library_id, status, version, scope, created_at, created_by,"
                " updated_at, updated_by) VALUES (?, 'active', ?, ?, ?, ?, ?, ?)",
                (library_id, version, scope_json, now, actor, now, actor),
            )
        event = db.execute(
            "INSERT INTO grant_events (library_id, action, version, scope, at, actor, actor_role,"
            " note, effective_at) VALUES (?, 'GRANT', ?, ?, ?, ?, ?, ?, ?)",
            (library_id, version, scope_json, now, actor, actor_role, note, now),
        )
        audit = SharingStore.append_audit(
            db, actor=actor, actor_role=actor_role,
            action="GRANT_UPDATE" if existing else "GRANT_CREATE",
            outcome="OK", library_id=library_id, grant_id=event.lastrowid,
            detail={"version": version, "scope": scope, "effective_at": now},
        )
        return SharingStore.grant_row(db, library_id), {
            "action": "GRANT_UPDATE" if existing else "GRANT_CREATE",
            "version": version, "effective_at": now, "changed": True, "audit": audit,
        }

    @staticmethod
    def revoke_grant(db, *, library_id: str, actor: str, actor_role: str,
                     note: str | None) -> tuple[dict, dict]:
        existing = SharingStore.grant_row(db, library_id)
        if not existing or existing["status"] != "active":
            raise LookupError("GRANT_NOT_ACTIVE")
        now = iso(utcnow())
        version = existing["version"] + 1
        db.execute(
            "UPDATE grants SET status='revoked', version=?, updated_at=?, updated_by=?"
            " WHERE library_id=?",
            (version, now, actor, library_id),
        )
        event = db.execute(
            "INSERT INTO grant_events (library_id, action, version, scope, at, actor, actor_role,"
            " note, effective_at) VALUES (?, 'REVOKE', ?, ?, ?, ?, ?, ?, ?)",
            (library_id, version, existing["scope"], now, actor, actor_role, note, now),
        )
        # 收回即刻关闭该馆尚未使用的下载凭据；已下载副本不动。
        closed = db.execute(
            "UPDATE tickets SET status='revoked', closed_at=?, closed_reason='GRANT_REVOKED'"
            " WHERE library_id=? AND status='active'",
            (now, library_id),
        ).rowcount
        audit = SharingStore.append_audit(
            db, actor=actor, actor_role=actor_role, action="GRANT_REVOKE", outcome="OK",
            library_id=library_id, grant_id=event.lastrowid,
            detail={"version": version, "revoked_tickets": closed, "effective_at": now},
        )
        return SharingStore.grant_row(db, library_id), {
            "action": "GRANT_REVOKE", "version": version, "effective_at": now,
            "revoked_tickets": closed, "audit": audit,
        }

    # ---- deliveries --------------------------------------------------
    @staticmethod
    def delivery_row(db, delivery_id: str) -> dict | None:
        row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def create_delivery(db, *, delivery_id: str, library_id: str, idempotency_key: str,
                        grant: dict, requested_by: str) -> tuple[dict, bool]:
        existing = db.execute(
            "SELECT * FROM deliveries WHERE library_id=? AND idempotency_key=?",
            (library_id, idempotency_key),
        ).fetchone()
        if existing:
            return dict(existing), False
        now = iso(utcnow())
        db.execute(
            "INSERT INTO deliveries (id, library_id, idempotency_key, grant_version, grant_scope,"
            " status, attempt, created_at, updated_at, requested_by)"
            " VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?)",
            (delivery_id, library_id, idempotency_key, grant["version"], grant["scope"],
             now, now, requested_by),
        )
        return SharingStore.delivery_row(db, delivery_id), True

    @staticmethod
    def record_ledger(db, *, delivery_id: str, library_id: str, items: int) -> bool:
        """按 delivery 唯一记账；重试不会产生第二笔。"""
        cur = db.execute(
            "INSERT OR IGNORE INTO delivery_ledger (delivery_id, library_id, items, recorded_at)"
            " VALUES (?, ?, ?, ?)",
            (delivery_id, library_id, items, iso(utcnow())),
        )
        return cur.rowcount == 1

    @staticmethod
    def ledger_for(db, delivery_id: str) -> dict | None:
        row = db.execute("SELECT * FROM delivery_ledger WHERE delivery_id=?", (delivery_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def ledger_totals(db) -> dict:
        rows = db.execute(
            "SELECT library_id, COUNT(*) AS entries, COALESCE(SUM(items),0) AS items"
            " FROM delivery_ledger GROUP BY library_id ORDER BY library_id"
        ).fetchall()
        total = db.execute(
            "SELECT COUNT(*) AS entries, COALESCE(SUM(items),0) AS items FROM delivery_ledger"
        ).fetchone()
        return {
            "by_library": [dict(r) for r in rows],
            "entries": total["entries"],
            "items": total["items"],
        }

    # ---- tickets / downloads ----------------------------------------
    @staticmethod
    def ticket_row(db, ticket_id: str) -> dict | None:
        row = db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def downloads_for(db, delivery_id: str) -> list[dict]:
        rows = db.execute("SELECT * FROM downloads WHERE delivery_id=? ORDER BY id", (delivery_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def tickets_for(db, delivery_id: str) -> list[dict]:
        rows = db.execute("SELECT * FROM tickets WHERE delivery_id=? ORDER BY issued_at, id", (delivery_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- read helpers ------------------------------------------------
    @staticmethod
    def audit_rows(db, *, library_id: str | None = None, delivery_id: str | None = None,
                   limit: int = 200) -> list[dict]:
        clauses, params = [], []
        if library_id:
            # 馆员只看到与本馆相关的记录；全局链校验另行进行。
            clauses.append("library_id=?")
            params.append(library_id)
        if delivery_id:
            clauses.append("delivery_id=?")
            params.append(delivery_id)
        sql = "SELECT * FROM audit"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.execute(sql, params).fetchall()]

    @staticmethod
    def all_grants(db) -> list[dict]:
        return [dict(r) for r in db.execute("SELECT * FROM grants ORDER BY library_id").fetchall()]

    @staticmethod
    def grant_events(db, library_id: str | None = None) -> list[dict]:
        if library_id:
            rows = db.execute("SELECT * FROM grant_events WHERE library_id=? ORDER BY id", (library_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM grant_events ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def deliveries(db, library_id: str | None = None, limit: int = 200) -> list[dict]:
        if library_id:
            rows = db.execute(
                "SELECT * FROM deliveries WHERE library_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (library_id, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM deliveries ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def summary(db, library_id: str | None = None) -> dict:
        out = {}
        for status in ("queued", "running", "delivered", "interrupted", "failed"):
            sql = "SELECT COUNT(*) FROM deliveries WHERE status=?"
            params = [status]
            if library_id:
                sql += " AND library_id=?"
                params.append(library_id)
            out[status] = db.execute(sql, params).fetchone()[0]
        return out

    @staticmethod
    def verify_audit_chain(db) -> dict:
        rows = [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id").fetchall()]
        prev = "GENESIS"
        for row in rows:
            if row["prev_hash"] != prev or audit_hash(prev, row) != row["hash"]:
                return {"ok": False, "checked": len(rows), "broken_at": row["id"]}
            prev = row["hash"]
        return {"ok": True, "checked": len(rows), "head": prev}


def digest_manifest(manifest: list[dict]) -> str:
    return canonical_digest(manifest)
