"""Manifested, quiesced backup and safe restore for a dedicated workspace."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import IO, TypedDict, cast

MAX_BACKUP_FILES = 10_000
MAX_BACKUP_BYTES = 2_147_483_648
MAX_MANIFEST_BYTES = 2_000_000
_FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)
_DATABASE_NAMES = ("workspace.sqlite3", "operational-data.sqlite", "actions.sqlite3")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class FileMetadata(TypedDict):
    sha256: str
    size: int


class BackupManifest(TypedDict):
    schema_version: int
    files: dict[str, FileMetadata]


def backup_workspace(root: str | Path, destination: str | Path) -> dict[str, object]:
    """Create online SQLite copies plus artifacts while holding the workspace lock."""
    workspace = _source_workspace(root)
    output = Path(destination).resolve()
    if output == workspace or workspace in output.parents:
        raise ValueError("Backup output must be outside the source workspace")
    output.parent.mkdir(parents=True, exist_ok=True)
    with _workspace_lock(workspace):
        database_paths = _database_paths(workspace)
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary_name:
            temporary = Path(temporary_name)
            sources: list[tuple[str, Path]] = []
            for name, source in database_paths:
                copy = temporary / name
                _backup_database(source, copy)
                sources.append((name, copy))
            sources.extend(_artifact_paths(workspace))
            _check_source_bounds(sources)
            files: dict[str, FileMetadata] = {
                name: {"sha256": _digest(path), "size": path.stat().st_size}
                for name, path in sources
            }
            manifest: BackupManifest = {"schema_version": 1, "files": files}
            archive_tmp = temporary / "workspace-backup.zip"
            with zipfile.ZipFile(archive_tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                _write_bytes(
                    archive, "manifest.json", json.dumps(manifest, sort_keys=True).encode()
                )
                for name, path in sources:
                    _write_file(archive, name, path)
            archive_tmp.replace(output)
    return cast(dict[str, object], manifest)


def restore_workspace(archive_path: str | Path, root: str | Path) -> dict[str, object]:
    """Stream-validate a complete archive in staging, then atomically install it."""
    archive_file = Path(archive_path).resolve(strict=True)
    requested_target = Path(root)
    if requested_target.is_symlink():
        raise ValueError("Restore target must not be a symlink")
    target = requested_target.resolve()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore target must not exist or must be empty")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-restore-", dir=target.parent))
    try:
        with zipfile.ZipFile(archive_file) as archive:
            infos = archive.infolist()
            _check_archive_bounds(infos)
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("Backup has duplicate members or no manifest")
            for info in infos:
                _safe_member(info)
            manifest = _read_manifest(archive)
            if set(names) != {"manifest.json", *manifest["files"]}:
                raise ValueError("Backup members do not match manifest")
            for name, metadata in manifest["files"].items():
                destination = stage.joinpath(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _extract_verified(archive, name, destination, metadata)
        for name in _DATABASE_NAMES:
            path = stage / name
            if path.exists():
                _validate_database(path, require_workspace_schema=name == "workspace.sqlite3")
        if not (stage / "workspace.sqlite3").is_file():
            raise ValueError("Backup omits workspace.sqlite3")
        if target.exists():
            target.rmdir()
        stage.replace(target)
        return cast(dict[str, object], manifest)
    except (OSError, KeyError, RuntimeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Invalid backup archive: {exc}") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _source_workspace(root: str | Path) -> Path:
    try:
        workspace = Path(root).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("Workspace does not exist") from exc
    if not workspace.is_dir():
        raise ValueError("Workspace must be a directory")
    return workspace


@contextmanager
def _workspace_lock(workspace: Path) -> Iterator[None]:
    lock_path = workspace / ".owner.lock"
    if lock_path.is_symlink():
        raise ValueError("Workspace owner lock must not be a symlink")
    handle: IO[bytes] = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Workspace is active; stop the service before backup") from exc
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _database_paths(workspace: Path) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = []
    for name in _DATABASE_NAMES:
        path = workspace / name
        if path.is_symlink():
            raise ValueError(f"Backup refuses symlink database: {name}")
        if path.exists():
            if not path.is_file():
                raise ValueError(f"Workspace database is not a regular file: {name}")
            paths.append((name, path))
    if not paths or paths[0][0] != "workspace.sqlite3":
        raise ValueError("Workspace database does not exist")
    return paths


def _artifact_paths(workspace: Path) -> list[tuple[str, Path]]:
    artifacts = workspace / "artifacts"
    if artifacts.is_symlink():
        raise ValueError("Backup refuses a symlink artifact root")
    sources: list[tuple[str, Path]] = []
    if artifacts.is_dir():
        for path in sorted(artifacts.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Backup refuses symlink artifact: {path}")
            if path.is_file():
                sources.append((f"artifacts/{path.relative_to(artifacts).as_posix()}", path))
    return sources


def _backup_database(source: Path, destination: Path) -> None:
    try:
        with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as database:
            with sqlite3.connect(destination) as backup:
                database.backup(backup)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Invalid SQLite database: {source.name}") from exc


def _check_source_bounds(sources: list[tuple[str, Path]]) -> None:
    if len(sources) > MAX_BACKUP_FILES:
        raise ValueError("Backup file count exceeds limit")
    if sum(path.stat().st_size for _, path in sources) > MAX_BACKUP_BYTES:
        raise ValueError("Backup content exceeds size limit")


def _check_archive_bounds(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_BACKUP_FILES + 1:
        raise ValueError("Backup file count exceeds limit")
    if sum(info.file_size for info in infos) > MAX_BACKUP_BYTES + MAX_MANIFEST_BYTES:
        raise ValueError("Backup content exceeds size limit")


def _read_manifest(archive: zipfile.ZipFile) -> BackupManifest:
    info = archive.getinfo("manifest.json")
    if info.file_size > MAX_MANIFEST_BYTES:
        raise ValueError("Backup manifest exceeds size limit")
    with archive.open(info) as handle:
        value = json.load(handle)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "files"}
        or not isinstance(value.get("schema_version"), int)
        or isinstance(value.get("schema_version"), bool)
        or value.get("schema_version") != 1
    ):
        raise ValueError("Unsupported backup manifest")
    files_value = value.get("files")
    if not isinstance(files_value, dict):
        raise ValueError("Backup manifest files must be an object")
    files: dict[str, FileMetadata] = {}
    for name, metadata_value in files_value.items():
        if not isinstance(name, str) or not _allowed_name(name):
            raise ValueError(f"Backup manifest contains disallowed file: {name!r}")
        if not isinstance(metadata_value, dict) or set(metadata_value) != {"sha256", "size"}:
            raise ValueError(f"Backup manifest metadata is invalid: {name}")
        digest = metadata_value.get("sha256")
        size = metadata_value.get("size")
        if (
            not isinstance(digest, str)
            or _DIGEST.fullmatch(digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= MAX_BACKUP_BYTES
        ):
            raise ValueError(f"Backup manifest metadata is invalid: {name}")
        files[name] = {"sha256": digest, "size": size}
    if "workspace.sqlite3" not in files:
        raise ValueError("Backup manifest omits workspace.sqlite3")
    return {"schema_version": 1, "files": files}


def _allowed_name(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or path.as_posix() != name:
        return False
    return name in _DATABASE_NAMES or (len(path.parts) > 1 and path.parts[0] == "artifacts")


def _safe_member(info: zipfile.ZipInfo) -> None:
    if not _allowed_name(info.filename) and info.filename != "manifest.json":
        raise ValueError(f"Unsafe backup member: {info.filename}")
    if info.is_dir():
        raise ValueError(f"Unsafe backup member: {info.filename}")
    mode = info.external_attr >> 16
    if mode & 0o170000 == 0o120000:
        raise ValueError(f"Backup contains symlink: {info.filename}")


def _extract_verified(
    archive: zipfile.ZipFile, name: str, destination: Path, metadata: FileMetadata
) -> None:
    digest = hashlib.sha256()
    size = 0
    with archive.open(name) as source, destination.open("wb") as output:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > metadata["size"] or size > MAX_BACKUP_BYTES:
                raise ValueError(f"Backup size mismatch: {name}")
            digest.update(block)
            output.write(block)
    if size != metadata["size"] or digest.hexdigest() != metadata["sha256"]:
        raise ValueError(f"Backup digest mismatch: {name}")


def _validate_database(path: Path, *, require_workspace_schema: bool) -> None:
    try:
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as database:
            row = database.execute("PRAGMA quick_check").fetchone()
            tables = {
                item[0]
                for item in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Backup database is invalid: {path.name}") from exc
    required = {"records", "requests", "audit_events"} if require_workspace_schema else set()
    if row is None or row[0] != "ok" or not required <= tables:
        raise ValueError(f"Backup database failed validation: {path.name}")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    return info


def _write_bytes(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    archive.writestr(_zip_info(name), content)


def _write_file(archive: zipfile.ZipFile, name: str, path: Path) -> None:
    with archive.open(_zip_info(name), "w") as output, path.open("rb") as source:
        shutil.copyfileobj(source, output, length=1024 * 1024)
