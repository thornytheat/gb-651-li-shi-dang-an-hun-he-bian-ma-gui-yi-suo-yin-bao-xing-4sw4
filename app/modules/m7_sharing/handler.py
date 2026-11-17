"""馆际共享 HTTP 入口：批次页、授权、交付、下载与审计。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from ...auth import optional_identity, require_identity, require_role, resolve_library
from . import service

router = APIRouter(tags=["馆际共享"])


def _settings(request: Request):
    return request.app.state.settings


def _identity(request: Request):
    return require_identity(request)


@router.get("/batch")
def batch_page(request: Request):
    identity = optional_identity(request)
    if identity is None:
        return RedirectResponse("/login?next=/batch", status_code=303)
    settings = _settings(request)
    data = service.list_deliveries(settings, identity)
    grant_view = service.list_grants(settings, identity)["grants"]
    archive_view = service.archive_view(settings, identity)
    audit = service.audit_view(settings, identity, limit=100)
    tickets = {
        d["delivery_id"]: service.get_delivery(settings, identity, d["delivery_id"])["tickets"]
        for d in data["deliveries"]
    }
    return request.app.state.templates.TemplateResponse(
        request, "batch.html",
        {"title": "馆际共享批次", "identity": identity.public(), "data": data,
         "grants": grant_view, "archive": archive_view, "audit": audit,
         "libraries": settings.libraries, "tickets": tickets},
    )


@router.get("/login")
def login_page(request: Request, next: str = "/batch"):
    if optional_identity(request) is not None:
        return RedirectResponse(next if next.startswith("/") else "/batch", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request, "login.html", {"title": "登录", "next": next},
    )


@router.post("/api/auth/login")
def login(request: Request, payload: dict = Body(default={})):
    from ... import auth

    settings = _settings(request)
    identity = auth.authenticate(settings, str(payload.get("username", "")),
                                 str(payload.get("password", "")))
    token = auth.issue_session(settings, identity)
    response = JSONResponse({"identity": identity.public()})
    response.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="strict", path="/",
        max_age=settings.session_ttl_seconds,
    )
    return response


@router.post("/api/auth/logout")
def logout(request: Request):
    response = JSONResponse({"ok": True})
    response.delete_cookie("archivecodec_session", path="/")
    return response


@router.get("/api/auth/whoami")
def whoami(request: Request):
    return {"identity": _identity(request).public()}


# ---- archive -------------------------------------------------------------
@router.get("/api/archive")
def api_archive(request: Request, library_id: str | None = Query(default=None)):
    resolve_library(_identity(request), library_id, _settings(request)) if library_id else None
    return service.archive_view(_settings(request), _identity(request))


# ---- grants --------------------------------------------------------------
@router.get("/api/grants")
def api_grants(request: Request, library_id: str | None = Query(default=None)):
    return service.list_grants(_settings(request), _identity(request), library_id)


@router.get("/api/grants/{library_id}")
def api_grant(request: Request, library_id: str):
    return service.get_grant(_settings(request), _identity(request), library_id)


@router.put("/api/grants/{library_id}")
def api_grant_put(request: Request, library_id: str, payload: dict = Body(default={})):
    identity = _identity(request)
    require_role(identity, "curator")
    archive_ids = payload.get("archive_ids")
    return service.put_grant(_settings(request), identity, library_id,
                             archive_ids, payload.get("note"))


@router.post("/api/grants/{library_id}/revoke")
def api_grant_revoke(request: Request, library_id: str, payload: dict = Body(default={})):
    identity = _identity(request)
    require_role(identity, "curator")
    return service.revoke_grant(_settings(request), identity, library_id, payload.get("note"))


# ---- deliveries ----------------------------------------------------------
@router.get("/api/deliveries")
def api_deliveries(request: Request, library_id: str | None = Query(default=None)):
    return service.list_deliveries(_settings(request), _identity(request), library_id)


@router.post("/api/deliveries")
def api_delivery_create(request: Request, payload: dict = Body(default={})):
    identity = _identity(request)
    require_role(identity, "library")
    library_id = resolve_library(identity, payload.get("library_id"), _settings(request))
    return service.create_delivery(
        _settings(request), identity, library_id,
        payload.get("idempotency_key"), payload.get("archive_ids"), payload.get("note"),
    )


@router.get("/api/deliveries/{delivery_id}")
def api_delivery(request: Request, delivery_id: str):
    return service.get_delivery(_settings(request), _identity(request), delivery_id)


@router.post("/api/deliveries/{delivery_id}/process")
def api_delivery_process(request: Request, delivery_id: str, payload: dict = Body(default={})):
    identity = _identity(request)
    interrupt = payload.get("interrupt_after")
    return service.process_delivery(_settings(request), identity, delivery_id,
                                    int(interrupt) if interrupt is not None else None)


@router.post("/api/deliveries/{delivery_id}/tickets")
def api_ticket_issue(request: Request, delivery_id: str):
    return service.issue_ticket(_settings(request), _identity(request), delivery_id)


@router.post("/api/tickets/{ticket_id}/download")
def api_ticket_download(request: Request, ticket_id: str):
    result = service.redeem_ticket(_settings(request), _identity(request), ticket_id)
    csv_text = service.delivery_csv(result["manifest"])
    response = Response(
        csv_text, media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={result['delivery']['delivery_id']}.csv",
            "X-Manifest-Digest": result["manifest_digest"],
        },
    )
    return response


# ---- audit ---------------------------------------------------------------
@router.get("/api/audit")
def api_audit(request: Request, library_id: str | None = Query(default=None),
              delivery_id: str | None = Query(default=None), limit: int = Query(default=200)):
    return service.audit_view(_settings(request), _identity(request), library_id,
                              delivery_id, limit)


@router.get("/api/audit/verify")
def api_audit_verify(request: Request):
    identity = _identity(request)
    require_role(identity, "auditor", "curator")
    return service.verify_chain(_settings(request), identity)


@router.get("/api/deliveries/{delivery_id}/reproduce")
def api_delivery_reproduce(request: Request, delivery_id: str):
    identity = _identity(request)
    require_role(identity, "auditor", "curator", "library")
    return service.reproduce(_settings(request), identity, delivery_id)


# ---- page-facing evidence (kept compatible with the existing /runs page) --
@router.get("/api/export/reproduced/{delivery_id}.csv")
def api_export_reproduced(request: Request, delivery_id: str):
    identity = _identity(request)
    require_role(identity, "auditor", "curator")
    result = service.reproduce(_settings(request), identity, delivery_id)
    return Response(
        service.delivery_csv(result["reproduction"]), media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={delivery_id}-reproduced.csv",
            "X-Manifest-Digest": result["manifest_digest"],
        },
    )
