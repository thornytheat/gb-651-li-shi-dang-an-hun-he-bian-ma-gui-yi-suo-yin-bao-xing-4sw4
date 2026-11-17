"""馆际共享：按馆授权、交付、下载凭据与审计复现。

关键取舍：
- 交付清单（manifest）在创建时按当时的授权冻结，审计员复现的是这份原授权快照，
  不会按现行权限重算。
- 任何“能否继续交付/能否下载”的判定都使用当前授权。授权收回后旧凭据立即失效；
  已下载副本不追缴。
- 授权变更、交付记账、凭据核验都在 SQLite 写事务里串行化，审计链的顺序就是
  并发操作的生效边界。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import secrets
from datetime import timedelta

from ... import archive as archive_loader
from ...auth import Identity
from ...errors import AccessDenied, Conflict, DomainError, NotFound
from ...manifest import canonical_digest
from ...timeutil import iso, parse, utcnow
from .store import SharingStore, digest_manifest, store_for

RESUME_STATUSES = {"queued", "running", "interrupted", "failed"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _scope_ids(grant: dict | None) -> list[str]:
    if not grant:
        return []
    try:
        scope = json.loads(grant.get("scope") or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(scope, dict):
        scope = scope.get("archive_ids", [])
    return [str(value) for value in scope]


def _grant_view(grant: dict | None) -> dict | None:
    if not grant:
        return None
    return {
        "library_id": grant["library_id"],
        "status": grant["status"],
        "version": grant["version"],
        "archive_ids": _scope_ids(grant),
        "created_at": grant["created_at"],
        "updated_at": grant["updated_at"],
        "updated_by": grant["updated_by"],
    }


def _delivery_view(row: dict) -> dict:
    manifest = json.loads(row["manifest"]) if row.get("manifest") else None
    return {
        "delivery_id": row["id"],
        "library_id": row["library_id"],
        "status": row["status"],
        "attempt": row["attempt"],
        "progress": row.get("progress", 0),
        "items": row["items"],
        "grant_version": row["grant_version"],
        "grant_scope": json.loads(row["grant_scope"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "manifest_digest": row["manifest_digest"],
        "reason": row["reason"],
        "requested_by": row["requested_by"],
        "manifest": manifest,
    }


def _ticket_view(row: dict) -> dict:
    return {
        "ticket_id": row["id"],
        "delivery_id": row["delivery_id"],
        "library_id": row["library_id"],
        "grant_version": row["grant_version"],
        "issued_at": row["issued_at"],
        "expires_at": row["expires_at"],
        "status": row["status"],
        "used_at": row["used_at"],
        "closed_at": row["closed_at"],
        "closed_reason": row["closed_reason"],
    }


def _archive_entry_map(settings) -> tuple[archive_loader.ArchiveSnapshot, dict]:
    snapshot = archive_loader.load(settings)
    return snapshot, snapshot.by_id()


def _require_library(identity: Identity, library_id: str) -> None:
    if identity.is_library and identity.library_id != library_id:
        raise AccessDenied("LIBRARY_MISMATCH", "馆员只能操作本馆数据")


def _read_library(identity: Identity, requested: str | None) -> str | None:
    """筛选条件：馆员只能看本馆；其他角色可指定或不指定。"""
    if identity.is_library:
        if requested and requested != identity.library_id:
            raise AccessDenied("LIBRARY_MISMATCH", "馆员只能访问本馆数据")
        return identity.library_id
    return requested or None


def _build_manifest(delivery_id: str, library_id: str, grant: dict,
                    entries: list[archive_loader.ArchiveEntry],
                    snapshot: archive_loader.ArchiveSnapshot) -> dict:
    return {
        "delivery_id": delivery_id,
        "library_id": library_id,
        "grant_version": grant["version"],
        "grant_scope": _scope_ids(grant),
        "authorized_at": grant["updated_at"],
        "archive": {
            "source_file": snapshot.source_file,
            "encoding": snapshot.encoding,
            "source_digest": snapshot.digest,
        },
        "items": [entry.public() for entry in entries],
    }


def delivery_csv(manifest: dict) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=["archive_id", "box_no", "title", "source_line", "source_file"],
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(manifest["items"])
    return out.getvalue()


def _assert_scope_covers(grant: dict | None, needed: list[str], *, code: str) -> None:
    if not grant or grant["status"] != "active":
        raise AccessDenied("GRANT_REVOKED", "该馆当前没有有效授权")
    scope = set(_scope_ids(grant))
    uncovered = sorted(set(needed) - scope)
    if uncovered:
        raise AccessDenied(code, "目标档案不在当前授权范围内：" + ",".join(uncovered))


# --------------------------------------------------------------------------
# archive / grants
# --------------------------------------------------------------------------
def archive_view(settings, identity: Identity) -> dict:
    snapshot, entries = _archive_entry_map(settings)
    if identity.is_library:
        with store_for(settings.db_path).read() as db:
            grant = SharingStore.grant_row(db, identity.library_id)
        allowed = set(_scope_ids(grant)) if grant and grant["status"] == "active" else set()
        rows = [entry.public() for entry in snapshot.entries if entry.archive_id in allowed]
    else:
        rows = [entry.public() for entry in snapshot.entries]
    return {
        "source_file": snapshot.source_file,
        "encoding": snapshot.encoding,
        "source_digest": snapshot.digest,
        "archive_count": len(snapshot.entries),
        "quarantine_count": len(snapshot.quarantine),
        "empty_count": snapshot.empty,
        "entries": rows,
        "archive_ids": sorted(snapshot.by_id()),
    }


def list_grants(settings, identity: Identity, library_id: str | None = None) -> dict:
    library_id = _read_library(identity, library_id)
    store = store_for(settings.db_path)
    with store.read() as db:
        if library_id:
            row = SharingStore.grant_row(db, library_id)
            grants = [row] if row else []
        else:
            grants = SharingStore.all_grants(db)
    return {"grants": [_grant_view(grant) for grant in grants]}


def get_grant(settings, identity: Identity, library_id: str) -> dict:
    _require_library(identity, library_id)
    store = store_for(settings.db_path)
    with store.read() as db:
        grant = SharingStore.grant_row(db, library_id)
    if not grant:
        raise NotFound("GRANT_NOT_FOUND", "该馆尚未授权")
    return {"grant": _grant_view(grant)}


def put_grant(settings, identity: Identity, library_id: str,
              archive_ids, note: str | None = None) -> dict:
    if identity.role != "curator":
        raise AccessDenied("ROLE_DENIED", "只有授权员可以变更授权")
    if library_id not in settings.libraries:
        raise DomainError("UNKNOWN_LIBRARY", "目标馆未登记")
    if not isinstance(archive_ids, list) or not archive_ids:
        raise DomainError("EMPTY_SCOPE", "授权范围不能为空")
    snapshot, entries = _archive_entry_map(settings)
    requested = sorted({str(value) for value in archive_ids})
    unknown = [value for value in requested if value not in entries]
    if unknown:
        raise DomainError("UNKNOWN_ARCHIVE", "档案不存在：" + ",".join(unknown))
    scope = [entry.archive_id for entry in snapshot.entries if entry.archive_id in requested]

    store = store_for(settings.db_path)
    with store.write() as db:
        grant, outcome = SharingStore.set_grant(
            db, library_id=library_id, scope=scope,
            actor=identity.username, actor_role=identity.role, note=note,
        )
    return {"grant": _grant_view(grant), "outcome": outcome}


def revoke_grant(settings, identity: Identity, library_id: str, note: str | None = None) -> dict:
    if identity.role != "curator":
        raise AccessDenied("ROLE_DENIED", "只有授权员可以收回授权")
    if library_id not in settings.libraries:
        raise NotFound("UNKNOWN_LIBRARY", "目标馆未登记")
    store = store_for(settings.db_path)
    try:
        with store.write() as db:
            grant, outcome = SharingStore.revoke_grant(
                db, library_id=library_id, actor=identity.username,
                actor_role=identity.role, note=note,
            )
    except LookupError:
        raise Conflict("GRANT_NOT_ACTIVE", "该馆没有可收回的有效授权") from None
    return {"grant": _grant_view(grant), "outcome": outcome}


# --------------------------------------------------------------------------
# deliveries
# --------------------------------------------------------------------------
def list_deliveries(settings, identity: Identity, library_id: str | None = None) -> dict:
    library_id = _read_library(identity, library_id)
    store = store_for(settings.db_path)
    with store.read() as db:
        rows = SharingStore.deliveries(db, library_id)
        totals = SharingStore.ledger_totals(db)
        summary = SharingStore.summary(db, library_id)
    return {
        "deliveries": [_delivery_view(row) for row in rows],
        "ledger": totals,
        "summary": summary,
    }


def create_delivery(settings, identity: Identity, library_id: str | None,
                    idempotency_key: str | None, archive_ids=None,
                    note: str | None = None) -> dict:
    if identity.role != "library":
        raise AccessDenied("ROLE_DENIED", "只有接收馆可以申请交付")
    if library_id and library_id != identity.library_id:
        raise AccessDenied("LIBRARY_MISMATCH", "馆员只能为本馆申请交付")
    library_id = identity.library_id
    if not idempotency_key or not str(idempotency_key).strip():
        raise DomainError("MISSING_IDEMPOTENCY_KEY", "交付请求必须带幂等键")
    idempotency_key = str(idempotency_key).strip()

    snapshot, entries = _archive_entry_map(settings)
    store = store_for(settings.db_path)
    delivery_id = "dlv-" + canonical_digest({
        "library_id": library_id,
        "idempotency_key": idempotency_key,
    })[:20]

    # 授权读取、范围校验、清单冻结与落库在同一个写事务里完成：并发收回要么在
    # 本交付之前生效（则本次创建被拒），要么在之后生效（则本交付保留原授权快照）。
    with store.write() as db:
        grant = SharingStore.grant_row(db, library_id)
        _assert_scope_covers(grant, [], code="SCOPE_DENIED")
        granted = _scope_ids(grant)
        if archive_ids:
            if not isinstance(archive_ids, list):
                raise DomainError("INVALID_SCOPE", "archive_ids 必须是数组")
            requested = sorted({str(value) for value in archive_ids})
            unknown = [value for value in requested if value not in entries]
            if unknown:
                raise DomainError("UNKNOWN_ARCHIVE", "档案不存在：" + ",".join(unknown))
            uncovered = sorted(set(requested) - set(granted))
            if uncovered:
                raise AccessDenied("SCOPE_DENIED", "目标档案不在当前授权范围内：" + ",".join(uncovered))
            chosen = requested
        else:
            chosen = granted
        chosen_entries = [entry for entry in snapshot.entries if entry.archive_id in set(chosen)]
        if not chosen_entries:
            raise DomainError("EMPTY_SCOPE", "交付范围为空")

        manifest = _build_manifest(delivery_id, library_id, grant, chosen_entries, snapshot)
        manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest_digest = digest_manifest(manifest)

        row, created = SharingStore.create_delivery(
            db, delivery_id=delivery_id, library_id=library_id,
            idempotency_key=idempotency_key, grant=grant,
            requested_by=identity.username,
        )
        if not created:
            if row["manifest_digest"] and row["manifest_digest"] != manifest_digest:
                raise Conflict("IDEMPOTENCY_MISMATCH", "同一幂等键对应了不同的交付范围")
            return {"delivery": _delivery_view(row), "created": False, "ledger_created": False}
        db.execute(
            "UPDATE deliveries SET manifest=?, manifest_digest=?, items=? WHERE id=?",
            (manifest_text, manifest_digest, len(manifest["items"]), delivery_id),
        )
        SharingStore.append_audit(
            db, actor=identity.username, actor_role=identity.role,
            action="DELIVERY_CREATE", outcome="OK", library_id=library_id,
            delivery_id=delivery_id,
            detail={
                "grant_version": grant["version"],
                "items": len(manifest["items"]),
                "manifest_digest": manifest_digest,
                "idempotency_key": idempotency_key,
                "note": note,
            },
        )
        row = SharingStore.delivery_row(db, delivery_id)
    return {"delivery": _delivery_view(row), "created": True, "ledger_created": False}


def get_delivery(settings, identity: Identity, delivery_id: str) -> dict:
    store = store_for(settings.db_path)
    with store.read() as db:
        row = SharingStore.delivery_row(db, delivery_id)
        if not row:
            raise NotFound("DELIVERY_NOT_FOUND", "交付不存在")
        _require_library(identity, row["library_id"])
        tickets = [_ticket_view(ticket) for ticket in SharingStore.tickets_for(db, delivery_id)]
        downloads = SharingStore.downloads_for(db, delivery_id)
        ledger = SharingStore.ledger_for(db, delivery_id)
    return {
        "delivery": _delivery_view(row),
        "tickets": tickets,
        "downloads": downloads,
        "ledger": ledger,
    }


def process_delivery(settings, identity: Identity, delivery_id: str,
                     interrupt_after: int | None = None) -> dict:
    if identity.role != "library":
        raise AccessDenied("ROLE_DENIED", "只有接收馆可以执行交付")
    store = store_for(settings.db_path)
    with store.read() as db:
        row = SharingStore.delivery_row(db, delivery_id)
        if not row:
            raise NotFound("DELIVERY_NOT_FOUND", "交付不存在")
        _require_library(identity, row["library_id"])
        if row["status"] == "delivered":
            ledger = SharingStore.ledger_for(db, delivery_id)
            return {"delivery": _delivery_view(row), "replayed": True,
                    "ledger_created": False, "ledger": ledger}
        if row["status"] not in RESUME_STATUSES:
            raise Conflict("DELIVERY_STATE", "当前状态不可执行")
        manifest = json.loads(row["manifest"])
        needed = [item["archive_id"] for item in manifest["items"]]
        progress = row.get("progress", 0)

    # 队列执行前重验当前权限。中断恢复走的也是这里。
    denied: AccessDenied | None = None
    with store.write() as db:
        row = SharingStore.delivery_row(db, delivery_id)
        grant = SharingStore.grant_row(db, row["library_id"])
        try:
            _assert_scope_covers(grant, needed, code="SCOPE_DENIED")
        except AccessDenied as exc:
            db.execute(
                "UPDATE deliveries SET status='failed', reason=?, updated_at=?, last_error=?"
                " WHERE id=?",
                (exc.code, iso(utcnow()), exc.message, delivery_id),
            )
            SharingStore.append_audit(
                db, actor=identity.username, actor_role=identity.role,
                action="DELIVERY_RESUME" if row.get("progress") else "DELIVERY_PROCESS",
                outcome="DENIED", library_id=row["library_id"], delivery_id=delivery_id,
                detail={"reason": exc.code, "needed": needed,
                        "current_grant_version": grant["version"] if grant else None},
            )
            denied = exc
        else:
            db.execute(
                "UPDATE deliveries SET status='running', attempt=attempt+1, updated_at=?"
                " WHERE id=?",
                (iso(utcnow()), delivery_id),
            )
            SharingStore.append_audit(
                db, actor=identity.username, actor_role=identity.role,
                action="DELIVERY_RESUME" if progress else "DELIVERY_PROCESS",
                outcome="OK", library_id=row["library_id"], delivery_id=delivery_id,
                detail={
                    "grant_version_snapshot": row["grant_version"],
                    "current_grant_version": grant["version"],
                    "resume_from": progress,
                    "items": len(needed),
                },
            )
    if denied is not None:
        raise denied

    total = len(needed)
    if interrupt_after is not None and progress < interrupt_after < total:
        stop_at = interrupt_after
    else:
        stop_at = total
    if stop_at < total:
        with store.write() as db:
            db.execute(
                "UPDATE deliveries SET status='interrupted', progress=?, updated_at=?, last_error=?"
                " WHERE id=?",
                (stop_at, iso(utcnow()), "SIMULATED_INTERRUPT", delivery_id),
            )
            SharingStore.append_audit(
                db, actor=identity.username, actor_role=identity.role,
                action="DELIVERY_INTERRUPTED", outcome="OK", library_id=row["library_id"],
                delivery_id=delivery_id,
                detail={"progress": stop_at, "items": total},
            )
            row = SharingStore.delivery_row(db, delivery_id)
        return {"delivery": _delivery_view(row), "replayed": False,
                "ledger_created": False, "ledger": None}

    denied = None
    ledger_created = False
    ledger = None
    with store.write() as db:
        row = SharingStore.delivery_row(db, delivery_id)
        if row["status"] == "delivered":
            return {"delivery": _delivery_view(row), "replayed": True,
                    "ledger_created": False,
                    "ledger": SharingStore.ledger_for(db, delivery_id)}
        grant = SharingStore.grant_row(db, row["library_id"])
        # 完成落账前再核一次：与授权收回并发时，谁先提交谁生效。
        try:
            _assert_scope_covers(grant, needed, code="SCOPE_DENIED")
        except AccessDenied as exc:
            db.execute(
                "UPDATE deliveries SET status='failed', reason=?, updated_at=?, last_error=?"
                " WHERE id=?",
                (exc.code, iso(utcnow()), exc.message, delivery_id),
            )
            SharingStore.append_audit(
                db, actor=identity.username, actor_role=identity.role,
                action="DELIVERY_COMPLETE", outcome="DENIED",
                library_id=row["library_id"], delivery_id=delivery_id,
                detail={"reason": exc.code, "current_grant_version": grant["version"] if grant else None},
            )
            denied = exc
        else:
            now = iso(utcnow())
            db.execute(
                "UPDATE deliveries SET status='delivered', progress=?, updated_at=?, reason=NULL,"
                " last_error=NULL WHERE id=?",
                (total, now, delivery_id),
            )
            ledger_created = SharingStore.record_ledger(
                db, delivery_id=delivery_id, library_id=row["library_id"], items=total,
            )
            SharingStore.append_audit(
                db, actor=identity.username, actor_role=identity.role,
                action="DELIVERY_COMPLETE", outcome="OK", library_id=row["library_id"],
                delivery_id=delivery_id,
                detail={
                    "items": total,
                    "ledger_created": ledger_created,
                    "manifest_digest": row["manifest_digest"],
                    "grant_version_snapshot": row["grant_version"],
                    "current_grant_version": grant["version"],
                },
            )
            row = SharingStore.delivery_row(db, delivery_id)
            ledger = SharingStore.ledger_for(db, delivery_id)
    if denied is not None:
        raise denied
    return {"delivery": _delivery_view(row), "replayed": False,
            "ledger_created": ledger_created, "ledger": ledger}


# --------------------------------------------------------------------------
# tickets / downloads
# --------------------------------------------------------------------------
def issue_ticket(settings, identity: Identity, delivery_id: str) -> dict:
    if identity.role != "library":
        raise AccessDenied("ROLE_DENIED", "只有接收馆可以申请下载凭据")
    store = store_for(settings.db_path)
    with store.write() as db:
        row = SharingStore.delivery_row(db, delivery_id)
        if not row:
            raise NotFound("DELIVERY_NOT_FOUND", "交付不存在")
        _require_library(identity, row["library_id"])
        if row["status"] != "delivered":
            raise Conflict("DELIVERY_NOT_DELIVERED", "交付尚未完成")
        grant = SharingStore.grant_row(db, row["library_id"])
        needed = [item["archive_id"] for item in json.loads(row["manifest"])["items"]]
        # 凭据按“签发时”的现行权限核发，并记录当时的授权版本。
        _assert_scope_covers(grant, needed, code="SCOPE_DENIED")
        now = utcnow()
        ticket_id = "tkt-" + secrets.token_hex(12)
        expires = iso(now + timedelta(seconds=settings.ticket_ttl_seconds))
        db.execute(
            "INSERT INTO tickets (id, delivery_id, library_id, grant_version, issued_at,"
            " expires_at, status) VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (ticket_id, delivery_id, row["library_id"], grant["version"], iso(now), expires),
        )
        SharingStore.append_audit(
            db, actor=identity.username, actor_role=identity.role, action="TICKET_ISSUE",
            outcome="OK", library_id=row["library_id"], delivery_id=delivery_id,
            ticket_id=ticket_id,
            detail={"grant_version": grant["version"],
                    "delivery_grant_version": row["grant_version"], "expires_at": expires},
        )
        ticket = SharingStore.ticket_row(db, ticket_id)
    return {"ticket": _ticket_view(ticket)}


def redeem_ticket(settings, identity: Identity, ticket_id: str) -> dict:
    if identity.role != "library":
        raise AccessDenied("ROLE_DENIED", "只有接收馆可以下载")
    store = store_for(settings.db_path)
    denial: Exception | None = None
    result: dict | None = None
    with store.write() as db:
        ticket = SharingStore.ticket_row(db, ticket_id)
        if not ticket:
            raise NotFound("TICKET_NOT_FOUND", "下载凭据不存在")
        _require_library(identity, ticket["library_id"])
        delivery = SharingStore.delivery_row(db, ticket["delivery_id"])
        grant = SharingStore.grant_row(db, ticket["library_id"])
        needed = [item["archive_id"] for item in json.loads(delivery["manifest"])["items"]] if delivery and delivery.get("manifest") else []

        def deny(code: str, message: str, *, close: str | None = None):
            """拒绝也要留下证据：先写审计/作废，提交后再抛出。"""
            nonlocal denial
            if close:
                db.execute(
                    "UPDATE tickets SET status='revoked', closed_at=?, closed_reason=? WHERE id=?",
                    (iso(utcnow()), close, ticket_id),
                )
            SharingStore.append_audit(
                db, actor=identity.username, actor_role=identity.role,
                action="DOWNLOAD", outcome="DENIED", library_id=ticket["library_id"],
                delivery_id=ticket["delivery_id"], ticket_id=ticket_id,
                detail={"reason": code, "message": message,
                        "current_grant_version": grant["version"] if grant else None},
            )
            denial = AccessDenied(code, message)

        if denial is None:
            if ticket["status"] == "revoked":
                deny("TICKET_REVOKED", "凭据已随授权收回失效")
            elif ticket["status"] == "used":
                denial = Conflict("TICKET_USED", "凭据已使用，不能重复下载")
            elif ticket["status"] == "expired" or parse(ticket["expires_at"]) <= utcnow():
                db.execute("UPDATE tickets SET status='expired', closed_at=?, closed_reason='TTL' WHERE id=?",
                           (iso(utcnow()), ticket_id))
                SharingStore.append_audit(
                    db, actor=identity.username, actor_role=identity.role,
                    action="DOWNLOAD", outcome="DENIED", library_id=ticket["library_id"],
                    delivery_id=ticket["delivery_id"], ticket_id=ticket_id,
                    detail={"reason": "TICKET_EXPIRED"},
                )
                denial = Conflict("TICKET_EXPIRED", "凭据已过期")
            elif not delivery or delivery["status"] != "delivered":
                deny("DELIVERY_NOT_DELIVERED", "交付未完成")
            else:
                try:
                    _assert_scope_covers(grant, needed, code="SCOPE_DENIED")
                except AccessDenied as exc:
                    deny(exc.code, exc.message,
                         close="SCOPE_REVOKED" if exc.code == "SCOPE_DENIED" else "GRANT_REVOKED")

        if denial is None:
            # 授权仍有效且覆盖交付物。单次凭据在此原子核销。
            updated = db.execute(
                "UPDATE tickets SET status='used', used_at=? WHERE id=? AND status='active'",
                (iso(utcnow()), ticket_id),
            ).rowcount
            if updated != 1:
                denial = Conflict("TICKET_CONFLICT", "凭据状态已被并发修改")
            else:
                download_at = iso(utcnow())
                manifest_digest = delivery["manifest_digest"]
                db.execute(
                    "INSERT INTO downloads (ticket_id, delivery_id, library_id, at, items, manifest_digest)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (ticket_id, ticket["delivery_id"], ticket["library_id"], download_at,
                     delivery["items"], manifest_digest),
                )
                SharingStore.append_audit(
                    db, actor=identity.username, actor_role=identity.role, action="DOWNLOAD",
                    outcome="OK", library_id=ticket["library_id"],
                    delivery_id=ticket["delivery_id"], ticket_id=ticket_id,
                    detail={
                        "items": delivery["items"], "manifest_digest": manifest_digest,
                        "ticket_grant_version": ticket["grant_version"],
                        "current_grant_version": grant["version"],
                        "boundary": "grant change effective at commit order in audit chain",
                    },
                )
                downloaded = SharingStore.ticket_row(db, ticket_id)
                result = {
                    "ticket": _ticket_view(downloaded),
                    "delivery": _delivery_view(delivery),
                    "manifest": json.loads(delivery["manifest"]),
                    "manifest_digest": manifest_digest,
                    "at": download_at,
                }
    if denial is not None:
        raise denial
    assert result is not None
    return result


# --------------------------------------------------------------------------
# audit / reproduction
# --------------------------------------------------------------------------
def audit_view(settings, identity: Identity, library_id: str | None = None,
               delivery_id: str | None = None, limit: int = 200) -> dict:
    library_id = _read_library(identity, library_id)
    store = store_for(settings.db_path)
    with store.read() as db:
        rows = SharingStore.audit_rows(db, library_id=library_id, delivery_id=delivery_id,
                                       limit=max(1, min(limit, 1000)))
        chain = SharingStore.verify_audit_chain(db)
    return {"audit": rows, "chain": chain}


def verify_chain(settings, identity: Identity) -> dict:
    store = store_for(settings.db_path)
    with store.read() as db:
        return SharingStore.verify_audit_chain(db)


def reproduce(settings, identity: Identity, delivery_id: str) -> dict:
    store = store_for(settings.db_path)
    with store.read() as db:
        row = SharingStore.delivery_row(db, delivery_id)
        if not row:
            raise NotFound("DELIVERY_NOT_FOUND", "交付不存在")
        _require_library(identity, row["library_id"])
        if identity.is_library and row["status"] != "delivered":
            raise AccessDenied("NOT_DELIVERED", "交付尚未完成，不能复现")
        manifest = json.loads(row["manifest"]) if row["manifest"] else None
        if not manifest:
            raise Conflict("MANIFEST_MISSING", "该交付没有冻结清单")
        recomputed = digest_manifest(manifest)
        grant = SharingStore.grant_row(db, row["library_id"])
        events = [event for event in SharingStore.grant_events(db, row["library_id"])
                  if event["version"] == row["grant_version"]]
        tickets = [_ticket_view(ticket) for ticket in SharingStore.tickets_for(db, delivery_id)]
        downloads = SharingStore.downloads_for(db, delivery_id)
        ledger = SharingStore.ledger_for(db, delivery_id)
        audit_rows = SharingStore.audit_rows(db, delivery_id=delivery_id, limit=1000)
        chain = SharingStore.verify_audit_chain(db)
    return {
        "delivery_id": delivery_id,
        "reproduction": manifest,
        "manifest_digest": row["manifest_digest"],
        "recomputed_digest": recomputed,
        "digest_ok": recomputed == row["manifest_digest"],
        "original_authorization": {
            "grant_version": row["grant_version"],
            "scope": json.loads(row["grant_scope"]),
            "events": events,
            "authorization_snapshot_preserved": True,
            "current_grant": _grant_view(grant),
        },
        "tickets": tickets,
        "downloads": downloads,
        "ledger": ledger,
        "audit": audit_rows,
        "chain": chain,
    }
