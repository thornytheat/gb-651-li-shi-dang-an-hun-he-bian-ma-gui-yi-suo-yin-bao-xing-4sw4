"""实际档案接入。

从随仓库样例或 `ARCHIVE_PATH` 指向的真实 CSV 读取档案。档号保持原样字符串，
行号沿用原文件物理行号；编码经 M2 严格判定后解码，不产生替换字符。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .modules.m1_intake.core import scan
from .modules.m2_encoding.core import detect

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = PACKAGE_ROOT / "sample_data"


@dataclass(frozen=True)
class ArchiveEntry:
    archive_id: str
    box_no: str
    title: str
    source_line: int
    source_file: str

    def public(self) -> dict:
        return {
            "archive_id": self.archive_id,
            "box_no": self.box_no,
            "title": self.title,
            "source_line": self.source_line,
            "source_file": self.source_file,
        }


@dataclass(frozen=True)
class ArchiveSnapshot:
    entries: list[ArchiveEntry]
    quarantine: list[dict]
    empty: int
    source_file: str
    encoding: str
    digest: str

    def by_id(self) -> dict[str, ArchiveEntry]:
        return {entry.archive_id: entry for entry in self.entries}


def _candidate(settings) -> Path:
    configured = getattr(settings, "archive_path", None)
    if not configured:
        return DEFAULT_DIR / "archive_dirty.csv"
    path = Path(configured)
    if path.is_dir():
        for name in ("archive_dirty.csv", "archive.csv"):
            if (path / name).is_file():
                return path / name
        csvs = sorted(path.glob("*.csv"))
        if not csvs:
            raise FileNotFoundError(f"目录中没有 CSV 档案：{path}")
        return csvs[0]
    return path


def _materialize_bytes(raw: bytes) -> bytes:
    """样例档案以字面量转义保存（单行、无真实换行）。仅在确实没有真实换行时
    还原转义，真实多行 CSV 原样使用，避免误改正文里的反斜杠。"""
    if b"\n" in raw or b"\r" in raw:
        return raw
    if b"\\n" not in raw:
        return raw
    text = raw.decode("utf-8", "strict")
    text = (text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")
            .replace("\\ufeff", "﻿"))
    return text.encode("utf-8")


def load(settings) -> ArchiveSnapshot:
    path = _candidate(settings)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        prepared = _materialize_bytes(raw)
    except UnicodeDecodeError as exc:
        raise ValueError(f"档案不是有效 UTF-8：{path}") from exc
    decoded = detect(prepared)
    if decoded.get("reason") != "OK":
        raise ValueError(f"档案编码无法判定：{path}")
    text = decoded["text"]
    parsed = scan(text)
    header = parsed["header"] or []
    index = {name: pos for pos, name in enumerate(header)}
    missing = [name for name in ("档号", "盒号", "题名") if name not in index]
    if missing:
        raise ValueError(f"档案缺少必要列：{','.join(missing)}")
    entries = [
        ArchiveEntry(
            archive_id=str(row["档号"]),
            box_no=str(row["盒号"]),
            title=str(row["题名"]),
            source_line=int(row["source_line"]),
            source_file=path.name,
        )
        for row in parsed["rows"]
    ]
    return ArchiveSnapshot(
        entries=entries,
        quarantine=parsed["quarantine"],
        empty=parsed["empty"],
        source_file=path.name,
        encoding=decoded["encoding"],
        digest=digest,
    )
