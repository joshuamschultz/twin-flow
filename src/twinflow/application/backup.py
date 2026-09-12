"""Manifested backup and safe restore for a dedicated local workspace."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import cast

from twinflow.application.repository import WorkspaceRepository

MAX_BACKUP_FILES = 10_000
MAX_BACKUP_BYTES = 2_147_483_648
_FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def backup_workspace(root: str | Path, destination: str | Path) -> dict[str, object]:
    """Create a consistent database copy plus bounded completed artifacts."""
    workspace = Path(root).resolve()
    if not (workspace / "workspace.sqlite3").is_file():
        raise ValueError("Workspace database does not exist")
    output = Path(destination).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary_name:
        temporary = Path(temporary_name)
        database_copy = temporary / "workspace.sqlite3"
        WorkspaceRepository(workspace).backup_to(database_copy)
        sources: list[tuple[str, Path]] = [("workspace.sqlite3", database_copy)]
        artifacts = workspace / "artifacts"
        if artifacts.is_dir():
            for path in sorted(artifacts.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Backup refuses symlink artifact: {path}")
                if path.is_file():
                    sources.append((f"artifacts/{path.relative_to(artifacts).as_posix()}", path))
        if len(sources) > MAX_BACKUP_FILES:
            raise ValueError("Backup file count exceeds limit")
        total = sum(path.stat().st_size for _, path in sources)
        if total > MAX_BACKUP_BYTES:
            raise ValueError("Backup content exceeds size limit")
        files = {
            name: {"sha256": _digest(path), "size": path.stat().st_size} for name, path in sources
        }
        manifest: dict[str, object] = {"schema_version": 1, "files": files}
        archive_tmp = temporary / "workspace-backup.zip"
        with zipfile.ZipFile(archive_tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            _write_member(archive, "manifest.json", json.dumps(manifest, sort_keys=True).encode())
            for name, path in sources:
                _write_member(archive, name, path.read_bytes())
        archive_tmp.replace(output)
    return manifest


def restore_workspace(archive_path: str | Path, root: str | Path) -> dict[str, object]:
    """Validate a complete archive in staging, then atomically install it."""
    archive_file = Path(archive_path).resolve()
    target = Path(root).resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must not exist or must be empty")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-restore-", dir=target.parent))
    try:
        with zipfile.ZipFile(archive_file) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_BACKUP_FILES + 1:
                raise ValueError("Backup file count exceeds limit")
            if sum(info.file_size for info in infos) > MAX_BACKUP_BYTES:
                raise ValueError("Backup content exceeds size limit")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("Backup has duplicate members or no manifest")
            for info in infos:
                _safe_member(info)
            manifest_value = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest_value, dict) or manifest_value.get("schema_version") != 1:
                raise ValueError("Unsupported backup manifest")
            files_value = manifest_value.get("files")
            if not isinstance(files_value, dict):
                raise ValueError("Backup manifest files must be an object")
            files = cast(dict[str, object], files_value)
            if set(names) != {"manifest.json", *files}:
                raise ValueError("Backup members do not match manifest")
            for name, metadata_value in files.items():
                metadata = cast(dict[str, object], metadata_value)
                content = archive.read(name)
                if len(content) != metadata.get("size") or hashlib.sha256(
                    content
                ).hexdigest() != metadata.get("sha256"):
                    raise ValueError(f"Backup digest mismatch: {name}")
                destination = stage.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
        _validate_database(stage / "workspace.sqlite3")
        if target.exists():
            target.rmdir()
        stage.replace(target)
        return cast(dict[str, object], manifest_value)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError(f"Invalid backup archive: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _safe_member(info: zipfile.ZipInfo) -> None:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts or "\\" in info.filename or info.is_dir():
        raise ValueError(f"Unsafe backup member: {info.filename}")
    mode = info.external_attr >> 16
    if mode & 0o170000 == 0o120000:
        raise ValueError(f"Backup contains symlink: {info.filename}")


def _validate_database(path: Path) -> None:
    if not path.is_file():
        raise ValueError("Backup omits workspace.sqlite3")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as database:
            row = database.execute("PRAGMA quick_check").fetchone()
            tables = {
                item[0]
                for item in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.DatabaseError as exc:
        raise ValueError("Backup database is invalid") from exc
    if row is None or row[0] != "ok" or not {"records", "requests", "audit_events"} <= tables:
        raise ValueError("Backup database failed validation")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    archive.writestr(info, content)
