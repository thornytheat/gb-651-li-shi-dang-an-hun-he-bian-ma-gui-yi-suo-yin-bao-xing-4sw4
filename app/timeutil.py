"""统一时间来源。所有持久化时间用 UTC ISO-8601，展示另附东八区。"""
from datetime import datetime, timedelta, timezone

try:  # 精简镜像可能没有 tzdata，退回固定偏移
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 环境相关
    LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def local_iso(value: str) -> str:
    return parse(value).astimezone(LOCAL_TZ).isoformat(timespec="seconds")
