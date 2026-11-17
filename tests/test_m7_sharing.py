import threading

import pytest


@pytest.fixture
def app_client(client):
    return client


def login(client, username, password):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["identity"]


@pytest.fixture
def actors(client):
    from fastapi.testclient import TestClient

    app = client.app
    clients = {}
    identities = {}
    for name, username, password in (
        ("curator", "curator", "curator-secret"),
        ("auditor", "auditor", "auditor-secret"),
        ("alpha", "lib-alpha", "alpha-secret"),
        ("beta", "lib-beta", "beta-secret"),
    ):
        actor = TestClient(app)
        identities[name] = login(actor, username, password)
        clients[name] = actor
    return {"clients": clients, "identities": identities}


def grant(curator, library, ids):
    response = curator.put(f"/api/grants/{library}", json={"archive_ids": ids})
    assert response.status_code == 200, response.text
    return response.json()


def deliver(alpha, key="k", archive_ids=None):
    payload = {"idempotency_key": key}
    if archive_ids is not None:
        payload["archive_ids"] = archive_ids
    response = alpha.post("/api/deliveries", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def ticket(alpha, delivery_id):
    response = alpha.post(f"/api/deliveries/{delivery_id}/tickets")
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def test_login_required_and_forged_session_rejected(client):
    assert client.get("/api/archive").status_code == 401
    assert client.get("/batch", follow_redirects=False).status_code == 303
    forged = client
    forged.cookies.set("archivecodec_session", "eyJzdWIiOiJsaWItYmV0YSJ9.fake")
    assert forged.get("/api/deliveries").status_code == 401
    assert forged.get("/api/deliveries").json()["error"] == "SESSION_INVALID"


def test_wrong_password_and_unknown_user_same_error(client):
    a = client.post("/api/auth/login", json={"username": "lib-beta", "password": "nope"})
    b = client.post("/api/auth/login", json={"username": "ghost", "password": "nope"})
    assert a.status_code == b.status_code == 401
    assert a.json()["error"] == b.json()["error"] == "LOGIN_FAILED"


def test_library_identity_cannot_be_switched_by_request(actors):
    alpha = actors["clients"]["alpha"]
    response = alpha.post("/api/deliveries", json={"idempotency_key": "x", "library_id": "lib-beta"})
    assert response.status_code == 403
    assert response.json()["error"] == "LIBRARY_MISMATCH"
    assert alpha.get("/api/grants/lib-beta").status_code == 403


def test_actual_archive_str_ids_and_source_lines(actors):
    curator = actors["clients"]["curator"]
    archive = curator.get("/api/archive").json()
    ids = [entry["archive_id"] for entry in archive["entries"]]
    assert ids == ["000123", "9007199254740993"]
    assert [entry["source_line"] for entry in archive["entries"]] == [2, 4]


def test_grant_scoped_delivery_frozen_manifest(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    created = deliver(alpha, "k1", ["000123"])
    assert created["created"] is True
    assert [item["archive_id"] for item in created["delivery"]["manifest"]["items"]] == ["000123"]
    assert created["delivery"]["manifest"]["grant_version"] == 1


def test_delivery_rejects_archive_outside_grant(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    grant(curator, "lib-alpha", ["000123"])
    response = alpha.post("/api/deliveries", json={"idempotency_key": "k2", "archive_ids": ["9007199254740993"]})
    assert response.status_code == 403
    assert response.json()["error"] == "SCOPE_DENIED"


def test_idempotent_delivery_and_single_ledger_entry(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    first = deliver(alpha, "k3")
    second = deliver(alpha, "k3")
    assert first["created"] is True and second["created"] is False
    delivery_id = first["delivery"]["delivery_id"]
    done = alpha.post(f"/api/deliveries/{delivery_id}/process", json={}).json()
    assert done["delivery"]["status"] == "delivered"
    assert done["ledger_created"] is True
    replay = alpha.post(f"/api/deliveries/{delivery_id}/process", json={}).json()
    assert replay["replayed"] is True and replay["ledger_created"] is False
    ledger = alpha.get("/api/deliveries").json()["ledger"]
    assert ledger["entries"] == 1 and ledger["items"] == 2


def test_interrupt_then_resume_revalidates_and_records_once(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    delivery_id = deliver(alpha, "k4")["delivery"]["delivery_id"]
    stopped = alpha.post(f"/api/deliveries/{delivery_id}/process", json={"interrupt_after": 1}).json()
    assert stopped["delivery"]["status"] == "interrupted"
    assert stopped["ledger_created"] is False

    curator.post("/api/grants/lib-alpha/revoke", json={})
    denied = alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    assert denied.status_code == 403
    assert denied.json()["error"] == "GRANT_REVOKED"
    status = alpha.get(f"/api/deliveries/{delivery_id}").json()["delivery"]["status"]
    assert status == "failed"

    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    resumed = alpha.post(f"/api/deliveries/{delivery_id}/process", json={}).json()
    assert resumed["delivery"]["status"] == "delivered"
    assert resumed["ledger_created"] is True
    again = alpha.post(f"/api/deliveries/{delivery_id}/process", json={}).json()
    assert again["ledger_created"] is False
    assert alpha.get("/api/deliveries").json()["ledger"]["entries"] == 1


def test_revoke_voids_old_ticket_but_keeps_downloaded_copy(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    auditor = actors["clients"]["auditor"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    delivery_id = deliver(alpha, "k5")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    first = ticket(alpha, delivery_id)
    assert alpha.post(f"/api/tickets/{first['ticket_id']}/download").status_code == 200

    outstanding = ticket(alpha, delivery_id)
    curator.post("/api/grants/lib-alpha/revoke", json={})
    denied = alpha.post(f"/api/tickets/{outstanding['ticket_id']}/download")
    assert denied.status_code == 403
    assert denied.json()["error"] == "TICKET_REVOKED"

    reproduction = auditor.get(f"/api/deliveries/{delivery_id}/reproduce").json()
    assert len(reproduction["downloads"]) == 1
    assert reproduction["digest_ok"] is True
    assert reproduction["original_authorization"]["current_grant"]["status"] == "revoked"
    assert reproduction["original_authorization"]["grant_version"] == 1


def test_existing_download_not_retroactively_revoked(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    auditor = actors["clients"]["auditor"]
    grant(curator, "lib-alpha", ["000123"])
    delivery_id = deliver(alpha, "k6")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    used = ticket(alpha, delivery_id)
    assert alpha.post(f"/api/tickets/{used['ticket_id']}/download").status_code == 200
    curator.post("/api/grants/lib-alpha/revoke", json={})

    audit = auditor.get("/api/audit").json()
    download_rows = [row for row in audit["audit"] if row["action"] == "DOWNLOAD" and row["outcome"] == "OK"]
    assert len(download_rows) == 1
    assert auditor.get("/api/audit/verify").json()["ok"] is True


def test_scope_shrink_blocks_outstanding_ticket(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    delivery_id = deliver(alpha, "k7")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    outstanding = ticket(alpha, delivery_id)
    grant(curator, "lib-alpha", ["000123"])
    denied = alpha.post(f"/api/tickets/{outstanding['ticket_id']}/download")
    assert denied.status_code == 403
    assert denied.json()["error"] == "SCOPE_DENIED"


def test_ticket_is_single_use(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    grant(curator, "lib-alpha", ["000123"])
    delivery_id = deliver(alpha, "k8")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    current = ticket(alpha, delivery_id)
    assert alpha.post(f"/api/tickets/{current['ticket_id']}/download").status_code == 200
    reuse = alpha.post(f"/api/tickets/{current['ticket_id']}/download")
    assert reuse.status_code == 409
    assert reuse.json()["error"] == "TICKET_USED"


def test_other_library_keeps_its_access_after_revoke(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    beta = actors["clients"]["beta"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    grant(curator, "lib-beta", ["000123"])
    curator.post("/api/grants/lib-alpha/revoke", json={})
    assert [entry["archive_id"] for entry in beta.get("/api/archive").json()["entries"]] == ["000123"]
    created = deliver(beta, "b1", ["000123"])
    assert created["delivery"]["status"] == "queued"
    assert beta.post("/api/deliveries", json={"idempotency_key": "b2", "archive_ids": ["9007199254740993"]}).status_code == 403


def test_beta_cannot_use_alpha_ticket(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    beta = actors["clients"]["beta"]
    grant(curator, "lib-alpha", ["000123"])
    delivery_id = deliver(alpha, "k9")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    current = ticket(alpha, delivery_id)
    assert beta.post(f"/api/tickets/{current['ticket_id']}/download").status_code == 403


def test_roles_are_separated(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    auditor = actors["clients"]["auditor"]
    grant(curator, "lib-alpha", ["000123"])
    assert alpha.put("/api/grants/lib-alpha", json={"archive_ids": ["000123"]}).status_code == 403
    assert curator.post("/api/deliveries", json={"idempotency_key": "x"}).status_code == 403
    delivery_id = deliver(alpha, "k10")["delivery"]["delivery_id"]
    assert auditor.post(f"/api/deliveries/{delivery_id}/process", json={}).status_code == 403
    assert auditor.get("/api/audit").status_code == 200


def test_reproduction_uses_original_authorization_not_current(actors):
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    auditor = actors["clients"]["auditor"]
    grant(curator, "lib-alpha", ["000123", "9007199254740993"])
    delivery_id = deliver(alpha, "k11")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    grant(curator, "lib-alpha", ["000123"])  # 收紧现权限，但不得改写原交付依据
    reproduction = auditor.get(f"/api/deliveries/{delivery_id}/reproduce").json()
    assert reproduction["digest_ok"] is True
    assert reproduction["reproduction"]["grant_scope"] == ["000123", "9007199254740993"]
    assert [item["archive_id"] for item in reproduction["reproduction"]["items"]] == ["000123", "9007199254740993"]
    assert reproduction["original_authorization"]["current_grant"]["archive_ids"] == ["000123"]
    exported = auditor.get(f"/api/export/reproduced/{delivery_id}.csv")
    assert exported.status_code == 200
    assert "000123" in exported.text and "9007199254740993" in exported.text


def test_audit_chain_detects_tampering(actors):
    from app.modules.m7_sharing.store import store_for

    curator = actors["clients"]["curator"]
    auditor = actors["clients"]["auditor"]
    grant(curator, "lib-alpha", ["000123"])
    assert auditor.get("/api/audit/verify").json()["ok"] is True
    store = store_for(curator.app.state.settings.db_path)
    with store.write() as db:
        db.execute("UPDATE audit SET detail='tampered' WHERE id=1")
    assert auditor.get("/api/audit/verify").json()["ok"] is False


def test_concurrent_revoke_and_download_have_one_boundary(actors):
    """并发时只有一个明确边界：审计链里 DOWNLOAD OK 要么在收回之前，要么被拒。"""
    curator = actors["clients"]["curator"]
    alpha = actors["clients"]["alpha"]
    auditor = actors["clients"]["auditor"]
    grant(curator, "lib-alpha", ["000123"])
    delivery_id = deliver(alpha, "k12")["delivery"]["delivery_id"]
    alpha.post(f"/api/deliveries/{delivery_id}/process", json={})
    ready = ticket(alpha, delivery_id)

    results = {}

    def redeem():
        response = alpha.post(f"/api/tickets/{ready['ticket_id']}/download")
        results["status"] = response.status_code

    def revoke():
        curator.post("/api/grants/lib-alpha/revoke", json={})

    threads = [threading.Thread(target=redeem), threading.Thread(target=revoke)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert "status" in results

    audit = auditor.get("/api/audit").json()["audit"]
    ok_rows = [row for row in audit if row["action"] == "DOWNLOAD" and row["outcome"] == "OK"]
    revoke_rows = [row for row in audit if row["action"] == "GRANT_REVOKE"]
    denied_rows = [row for row in audit if row["action"] == "DOWNLOAD" and row["outcome"] == "DENIED"]
    assert len(revoke_rows) == 1
    if results["status"] == 200:
        # 下载在收回提交前生效；之后的旧凭据不可再用。
        assert len(ok_rows) == 1
        assert ok_rows[0]["id"] < revoke_rows[0]["id"]
    else:
        # 收回先提交；下载被拒并留痕，不存在“既下载成功又已收回”的中间态。
        assert results["status"] == 403
        assert ok_rows == []
        assert len(denied_rows) == 1
    assert auditor.get("/api/audit/verify").json()["ok"] is True
