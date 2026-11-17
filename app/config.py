"""业务应用包。"""
from dataclasses import dataclass, field
import json
import os

DEFAULT_USERS = {
    "curator": {
        "password": "curator-secret",
        "role": "curator",
        "name": "馆际授权员",
    },
    "auditor": {
        "password": "auditor-secret",
        "role": "auditor",
        "name": "交付审计员",
    },
    "lib-alpha": {
        "password": "alpha-secret",
        "role": "library",
        "library_id": "lib-alpha",
        "name": "甲馆",
    },
    "lib-beta": {
        "password": "beta-secret",
        "role": "library",
        "library_id": "lib-beta",
        "name": "乙馆",
    },
}


def _users_from_env() -> dict:
    raw = os.getenv("AUTH_USERS")
    if not raw:
        return {key: dict(value) for key, value in DEFAULT_USERS.items()}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("AUTH_USERS 必须是 JSON 对象")
    return parsed


@dataclass(frozen=True)
class Settings:
    db_path: str = "./data/archivecodec.db"
    rule_version: str = "2026.11"
    timezone: str = "Asia/Shanghai"
    auth_secret: str = "dev-only-change-me-2026.11"
    session_ttl_seconds: int = 3600
    ticket_ttl_seconds: int = 86400
    archive_path: str | None = None
    users: dict = field(default_factory=_users_from_env)

    @classmethod
    def from_env(cls):
        users = _users_from_env()
        archive_path = os.getenv("ARCHIVE_PATH") or None
        return cls(
            db_path=os.getenv("DB_PATH", cls.db_path),
            rule_version=os.getenv("RULE_VERSION", cls.rule_version),
            timezone=os.getenv("APP_TIMEZONE", cls.timezone),
            auth_secret=os.getenv("AUTH_SECRET", cls.auth_secret),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", cls.session_ttl_seconds)),
            ticket_ttl_seconds=int(os.getenv("TICKET_TTL_SECONDS", cls.ticket_ttl_seconds)),
            archive_path=archive_path,
            users=users,
        )

    @property
    def libraries(self) -> dict:
        """返回 library_id -> 显示名称。"""
        out = {}
        for username, user in self.users.items():
            if user.get("role") == "library" and user.get("library_id"):
                out[user["library_id"]] = user.get("name", user["library_id"])
        return out
