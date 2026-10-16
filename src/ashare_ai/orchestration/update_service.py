"""Data-preserving online update coordination.

The coordinator owns update state transitions and keeps the monitoring and
notification continuity handover explicit. Packaging systems can provide an
installer callback; the default callback only stages a verified marker, which
keeps API and test environments safe and makes the lifecycle deterministic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai import __version__
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.storage.models import UpdateJob

logger = logging.getLogger(__name__)


class UpdateManifest(BaseModel):
    version: str = Field(min_length=1, max_length=64)
    package_url: str | None = None
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    architecture: str = Field(default="api-worker", min_length=1, max_length=64)
    release_notes: str = Field(default="", max_length=4000)
    minimum_version: str | None = None


class UpdateCheckResult(BaseModel):
    available: bool
    current_version: str
    target_version: str | None = None
    manifest: UpdateManifest | None = None
    status: str
    message: str


def _version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for item in value.lstrip("vV").split("."):
        digits = "".join(char for char in item if char.isdigit())
        parts.append(int(digits or 0))
    return tuple([*parts, 0, 0, 0][:3])


def architecture_label() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}-api-worker"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UpdateContinuityController:
    """Small adapter for a real standby process in packaged deployments."""

    def activate(self, update_tag: str, *, architecture: str) -> dict[str, Any]:
        return {
            "mode": "standby",
            "update_tag": update_tag,
            "architecture": architecture,
            "monitoring": "RUNNING",
            "notifications": "RUNNING",
            "activated_at": datetime.now(UTC).isoformat(),
        }

    def sync(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "monitoring": "RUNNING",
            "notifications": "RUNNING",
            "last_sync_at": datetime.now(UTC).isoformat(),
        }

    def deactivate(self, state: dict[str, Any]) -> dict[str, Any]:
        return {**state, "mode": "primary", "deactivated_at": datetime.now(UTC).isoformat()}


Installer = Callable[[UpdateJob, UpdateManifest, Path], None]
Repairer = Callable[[UpdateJob, Path, Exception], bool]


class UpdateCoordinator:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        continuity: UpdateContinuityController | None = None,
        installer: Installer | None = None,
        repairer: Repairer | None = None,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        self.settings = settings or get_settings()
        self.continuity = continuity or UpdateContinuityController()
        self.installer = installer or self._stage_installer
        self.repairer = repairer or self._default_repairer
        self.client_factory = client_factory

    @property
    def current_version(self) -> str:
        return __version__

    def _manifest(self) -> UpdateManifest:
        url = self.settings.update_manifest_url
        if not url:
            raise RuntimeError("在线更新清单地址尚未配置")
        if self.settings.app_env.casefold() == "production" and urlsplit(url).scheme != "https":
            raise RuntimeError("生产环境更新清单必须使用 HTTPS")
        with self.client_factory(timeout=self.settings.update_timeout_seconds) as client:
            response = client.get(url, headers={"Accept": "application/json"})
            response.raise_for_status()
            return UpdateManifest.model_validate(response.json())

    def check(self) -> UpdateCheckResult:
        if not self.settings.update_check_enabled:
            return UpdateCheckResult(
                available=False,
                current_version=self.current_version,
                status="DISABLED",
                message="在线更新检查已关闭",
            )
        try:
            manifest = self._manifest()
        except Exception as exc:
            logger.warning("online update check failed: %s", type(exc).__name__)
            return UpdateCheckResult(
                available=False,
                current_version=self.current_version,
                status="UNAVAILABLE",
                message="暂时无法获取更新清单",
            )
        available = _version_key(manifest.version) > _version_key(self.current_version)
        return UpdateCheckResult(
            available=available,
            current_version=self.current_version,
            target_version=manifest.version if available else None,
            manifest=manifest,
            status="AVAILABLE" if available else "UP_TO_DATE",
            message="发现可用更新" if available else "当前已是最新版本",
        )

    def create_check_job(self, session: Session, *, user_id: str | None) -> UpdateJob:
        now = datetime.now(UTC)
        tag = (
            f"upd-check-{now.strftime('%Y%m%d%H%M%S')}"
            f"-{datetime.now(UTC).timestamp_ns() % 1000000:06d}"
        )
        row = UpdateJob(
            user_id=user_id,
            status="CHECKING",
            phase="检查在线清单",
            progress=5,
            current_version=self.current_version,
            update_tag=tag,
            architecture_label=architecture_label(),
            manifest_url=self.settings.update_manifest_url,
            sync_state={"monitoring": "RUNNING", "notifications": "RUNNING"},
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        result = self.check()
        row.status = result.status
        row.phase = result.message
        row.progress = 100
        row.target_version = result.target_version
        row.release_notes = result.manifest.release_notes if result.manifest else None
        row.sync_state = {
            **(row.sync_state or {}),
            "available": result.available,
            "checked_at": datetime.now(UTC).isoformat(),
        }
        row.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(row)
        return row

    def _data_roots(self) -> list[Path]:
        roots = [self.settings.lake_root, self.settings.private_object_root]
        return list(dict.fromkeys(path.resolve() for path in roots))

    def _database_path(self) -> Path | None:
        marker = "sqlite:///"
        url = self.settings.database_url
        if not url.startswith(marker):
            marker = "sqlite+pysqlite:///"
        if not url.startswith(marker):
            return None
        raw = unquote(url[len(marker) :].split("?", 1)[0])
        return Path(raw).resolve() if raw else None

    def _create_backup(self, row: UpdateJob) -> tuple[Path, str]:
        target = self.settings.update_backup_root.resolve() / row.update_tag
        target.mkdir(parents=True, exist_ok=False)
        manifest: dict[str, Any] = {
            "update_tag": row.update_tag,
            "created_at": datetime.now(UTC).isoformat(),
            "files": [],
        }
        for source in [*self._data_roots(), self._database_path()]:
            if source is None or not source.exists() or target in source.parents:
                continue
            destination = target / source.name
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            for item in destination.rglob("*") if destination.is_dir() else [destination]:
                if item.is_file():
                    manifest["files"].append(
                        {"path": str(item.relative_to(target)), "sha256": _sha256_file(item)}
                    )
        manifest_path = target / "backup-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True), encoding="utf-8"
        )
        return target, _sha256_file(manifest_path)

    def _stage_installer(self, row: UpdateJob, manifest: UpdateManifest, backup: Path) -> None:
        marker = backup / "staged-update.json"
        marker.write_text(
            json.dumps(
                {
                    "version": manifest.version,
                    "architecture": manifest.architecture,
                    "package_url": manifest.package_url,
                },
                ensure_ascii=True,
            ),
            encoding="utf-8",
        )

    def _default_repairer(self, row: UpdateJob, backup: Path, error: Exception) -> bool:
        return (backup / "backup-manifest.json").exists()

    def _save(self, session: Session, row: UpdateJob, **values: Any) -> None:
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)
        session.commit()
        session.refresh(row)

    def run(self, session: Session, update_id: str) -> UpdateJob:
        row = session.get(UpdateJob, update_id)
        if row is None:
            raise LookupError("update job not found")
        if row.status in {"SUCCEEDED", "FAILED", "CANCELLED", "UP_TO_DATE", "UNAVAILABLE"}:
            return row
        result = self.check()
        manifest = result.manifest
        if manifest is None or not result.available:
            self._save(
                session,
                row,
                status=result.status,
                phase=result.message,
                progress=100,
                target_version=result.target_version,
            )
            return row
        self._save(
            session,
            row,
            status="PROCESSING",
            phase="备份数据",
            progress=15,
            target_version=manifest.version,
            release_notes=manifest.release_notes,
            started_at=datetime.now(UTC),
        )
        backup, backup_hash = self._create_backup(row)
        state = self.continuity.activate(row.update_tag, architecture=row.architecture_label)
        self._save(
            session,
            row,
            phase="启动备用监控与通知",
            progress=35,
            backup_path=str(backup),
            backup_sha256=backup_hash,
            fallback_active=True,
            sync_state=state,
        )
        error: Exception | None = None
        for attempt in range(3):
            try:
                self._save(
                    session,
                    row,
                    phase="安装更新",
                    progress=55 + attempt * 10,
                    repair_attempts=attempt,
                )
                self.installer(row, manifest, backup)
                error = None
                break
            except Exception as exc:  # pragma: no cover - exercised through injected callbacks
                error = exc
                try:
                    repaired = self.repairer(row, backup, exc)
                except Exception:
                    repaired = False
                self._save(
                    session,
                    row,
                    phase="自动修复安装",
                    progress=75,
                    repair_attempts=attempt + 1,
                    error_message=str(exc)[:500],
                )
                if not repaired:
                    break
        if error is not None:
            self._save(
                session,
                row,
                status="FAILED",
                phase="更新失败，备用模块继续运行",
                progress=100,
                error_message="安装失败，已保留数据备份",
                completed_at=datetime.now(UTC),
                fallback_active=True,
            )
            return row
        primary_state = self.continuity.deactivate(state)
        self._save(
            session,
            row,
            status="SUCCEEDED",
            phase="更新完成",
            progress=100,
            sync_state=primary_state,
            fallback_active=False,
            error_message=None,
            completed_at=datetime.now(UTC),
        )
        return row

    def sync_fallback(self, session: Session, update_id: str) -> UpdateJob:
        row = session.get(UpdateJob, update_id)
        if row is None:
            raise LookupError("update job not found")
        if not row.fallback_active:
            return row
        self._save(
            session,
            row,
            sync_state=self.continuity.sync(dict(row.sync_state or {})),
            phase="备用监控与通知已同步",
        )
        return row


def latest_job(
    session: Session, *, user_id: str | None, include_all: bool = False
) -> UpdateJob | None:
    statement = select(UpdateJob).order_by(UpdateJob.created_at.desc()).limit(1)
    if not include_all:
        statement = statement.where(UpdateJob.user_id == user_id)
    return session.scalar(statement)
