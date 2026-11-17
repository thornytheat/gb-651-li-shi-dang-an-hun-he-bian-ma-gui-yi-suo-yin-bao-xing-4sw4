"""可信登录与会话。

登录成功后签发 HMAC 签名的不透明令牌。身份字段（角色、所属馆）全部由服务端配置
派生并写入签名载荷；客户端无法通过改 Cookie、改请求体或改路径把自己换成别的馆。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Request

from .config import Settings
from .errors import AccessDenied, Unauthenticated
from .timeutil import iso, parse, utcnow

COOKIE_NAME = "archivecodec_session"
SESSION_VERSION = "s1"


@dataclass(frozen=True)
class Identity:
    username: str
    role: str
    name: str
    library_id: str | None = None

    @property
    def is_curator(self) -> bool:
        return self.role == "curator"

    @property
    def is_auditor(self) -> bool:
        return self.role == "auditor"

    @property
    def is_library(self) -> bool:
        return self.role == "library"

    def public(self) -> dict:
        return {
            "username": self.username,
            "role": self.role,
            "name": self.name,
            "library_id": self.library_id,
        }


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(secret: str, payload: bytes) -> str:
    return _b64e(hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest())


def authenticate(settings: Settings, username: str, password: str) -> Identity:
    """校验口令。失败不区分“用户不存在”和“口令错误”，避免枚举。"""
    record = settings.users.get(username or "")
    if not record or not hmac.compare_digest(str(record.get("password", "")), password or ""):
        raise Unauthenticated("LOGIN_FAILED", "用户名或口令不正确")
    role = record.get("role")
    if role not in {"curator", "auditor", "library"}:
        raise Unauthenticated("LOGIN_FAILED", "账户角色未登记")
    library_id = record.get("library_id") if role == "library" else None
    if role == "library" and not library_id:
        raise Unauthenticated("LOGIN_FAILED", "馆员账户缺少所属馆")
    return Identity(username=username, role=role, name=record.get("name", username), library_id=library_id)


def issue_session(settings: Settings, identity: Identity) -> str:
    now = utcnow()
    claims = {
        "v": SESSION_VERSION,
        "sub": identity.username,
        "role": identity.role,
        "name": identity.name,
        "lib": identity.library_id,
        "iat": iso(now),
        "exp": iso(now + timedelta(seconds=settings.session_ttl_seconds)),
        "nonce": secrets.token_urlsafe(12),
    }
    payload = json.dumps(claims, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{_b64e(payload)}.{_sign(settings.auth_secret, payload)}"


def read_session(settings: Settings, token: str | None) -> Identity:
    if not token:
        raise Unauthenticated("SESSION_REQUIRED", "请先登录")
    try:
        payload_b64, signature = token.split(".", 1)
        payload = _b64d(payload_b64)
    except Exception:
        raise Unauthenticated("SESSION_INVALID", "会话令牌格式无效") from None
    expected = _sign(settings.auth_secret, payload)
    if not hmac.compare_digest(expected, signature):
        raise Unauthenticated("SESSION_INVALID", "会话令牌签名无效")
    try:
        claims = json.loads(payload.decode("utf-8"))
    except Exception:
        raise Unauthenticated("SESSION_INVALID", "会话载荷无效") from None
    if claims.get("v") != SESSION_VERSION:
        raise Unauthenticated("SESSION_INVALID", "会话版本不受支持")
    try:
        expired = parse(claims["exp"]) <= utcnow()
    except Exception:
        raise Unauthenticated("SESSION_INVALID", "会话有效期无效") from None
    if expired:
        raise Unauthenticated("SESSION_EXPIRED", "会话已过期，请重新登录")
    record = settings.users.get(claims.get("sub", ""))
    if not record:
        raise Unauthenticated("SESSION_STALE", "账户已不存在")
    role = record.get("role")
    library_id = record.get("library_id") if role == "library" else None
    if claims.get("role") != role or claims.get("lib") != library_id:
        # 角色/所属馆以服务端配置为准；令牌里的旧身份不再被采信。
        raise Unauthenticated("SESSION_STALE", "账户权限已变更，请重新登录")
    return Identity(username=claims["sub"], role=role, name=record.get("name", claims["sub"]), library_id=library_id)


def session_token_from_request(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def optional_identity(request: Request) -> Identity | None:
    token = session_token_from_request(request)
    if not token:
        return None
    return read_session(request.app.state.settings, token)


def require_identity(request: Request) -> Identity:
    return read_session(request.app.state.settings, session_token_from_request(request))


def require_role(identity: Identity, *roles: str) -> None:
    if identity.role not in roles:
        raise AccessDenied("ROLE_DENIED", "当前身份无权执行该操作")


def resolve_library(identity: Identity, requested: str | None, settings: Settings) -> str:
    """确定操作目标馆。馆员只能操作本馆；传别的馆一律拒绝，绝不静默替换。"""
    if identity.is_library:
        if requested and requested != identity.library_id:
            raise AccessDenied("LIBRARY_MISMATCH", "馆员只能访问本馆数据")
        return identity.library_id  # type: ignore[return-value]
    if not requested:
        raise AccessDenied("LIBRARY_REQUIRED", "请指定目标馆")
    if requested not in settings.libraries:
        raise AccessDenied("UNKNOWN_LIBRARY", "目标馆未登记")
    return requested
