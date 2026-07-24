from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, uuid5

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from sqlalchemy import select, text
from sqlalchemy.inspection import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session

from ashare_ai import __version__
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import canonical_json, sha256_bytes, stable_hash
from ashare_ai.storage.models import (
    AgentCall,
    AIChatMessage,
    AIChatThread,
    AuditEvent,
    AutomaticResearchReportConfig,
    BacktestRun,
    CandidateRow,
    EvidenceRow,
    ExitAdviceRow,
    JobRun,
    PortfolioRow,
    ReportRow,
    ScoreRow,
    SnapshotManifestRow,
    TradePlanRow,
    UserAssetState,
    UserResearchPreference,
)
from ashare_ai.storage.objects import S3ObjectStore

ARCHIVE_MAGIC = b"ASHARE-PERSONAL-ARCHIVE\n"
ARCHIVE_FORMAT = "ashare-personal-profile"
ARCHIVE_VERSION = 1
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 10_000


class PersonalArchiveError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def wrap_job_secret(
    passphrase: str, archive_id: str, settings: Settings | None = None
) -> str:
    key_id, key = _server_keys(settings or get_settings())[0]
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, passphrase.encode("utf-8"), archive_id.encode())
    return f"{key_id}.{base64.urlsafe_b64encode(nonce + ciphertext).decode('ascii')}"


def unwrap_job_secret(
    wrapped: str, archive_id: str, settings: Settings | None = None
) -> str:
    try:
        key_id, encoded = wrapped.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
        for candidate_id, key in _server_keys(settings or get_settings()):
            if candidate_id == key_id:
                plaintext = AESGCM(key).decrypt(
                    payload[:12], payload[12:], archive_id.encode()
                )
                return plaintext.decode("utf-8")
    except (ValueError, UnicodeDecodeError, InvalidTag) as exc:
        raise PersonalArchiveError(
            "档案一次性口令不可用", code="ARCHIVE_SECRET_UNAVAILABLE"
        ) from exc
    raise PersonalArchiveError(
        "档案一次性口令不可用", code="ARCHIVE_SECRET_UNAVAILABLE"
    )


def build_personal_archive(session: Session, user_id: str, passphrase: str) -> bytes:
    profile, object_payloads = _collect_profile(session, user_id)
    files: dict[str, bytes] = {
        "domain/profile.json": canonical_json(profile),
        **{f"objects/{digest}": payload for digest, payload in object_payloads.items()},
    }
    manifest: dict[str, Any] = {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_VERSION,
        "source_version": __version__,
        "alembic_revision": _alembic_revision(session),
        "created_at": datetime.now(UTC).isoformat(),
        "files": {
            name: {"sha256": sha256_bytes(payload), "byte_size": len(payload)}
            for name, payload in sorted(files.items())
        },
    }
    key_id, signing_key = _server_keys(get_settings())[0]
    manifest["source_auth"] = {
        "algorithm": "HMAC-SHA256",
        "key_id": key_id,
        "tag": hmac.new(
            signing_key,
            canonical_json(manifest),
            hashlib.sha256,
        ).hexdigest(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", canonical_json(manifest))
        for name, payload in sorted(files.items()):
            archive.writestr(name, payload)
    return encrypt_archive(buffer.getvalue(), passphrase)


def encrypt_archive(payload: bytes, passphrase: str) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    header = {
        "format": ARCHIVE_FORMAT,
        "version": ARCHIVE_VERSION,
        "cipher": "AES-256-GCM",
        "kdf": {"name": "scrypt", "n": 32768, "r": 8, "p": 1},
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "nonce": base64.urlsafe_b64encode(nonce).decode("ascii"),
    }
    header_bytes = canonical_json(header)
    key = _derive_archive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, payload, ARCHIVE_MAGIC + header_bytes)
    return ARCHIVE_MAGIC + header_bytes + b"\n" + ciphertext


def decrypt_archive(payload: bytes, passphrase: str) -> dict[str, bytes]:
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise PersonalArchiveError("档案超过大小上限", code="ARCHIVE_TOO_LARGE")
    if not payload.startswith(ARCHIVE_MAGIC):
        raise PersonalArchiveError("档案格式无效", code="ARCHIVE_FORMAT_INVALID")
    try:
        header_end = payload.index(b"\n", len(ARCHIVE_MAGIC))
        header_bytes = payload[len(ARCHIVE_MAGIC) : header_end]
        header = json.loads(header_bytes)
        if header.get("format") != ARCHIVE_FORMAT or int(header.get("version", 0)) != 1:
            raise PersonalArchiveError(
                "档案版本不兼容", code="ARCHIVE_VERSION_UNSUPPORTED"
            )
        salt = base64.urlsafe_b64decode(str(header["salt"]).encode("ascii"))
        nonce = base64.urlsafe_b64decode(str(header["nonce"]).encode("ascii"))
        key = _derive_archive_key(passphrase, salt)
        plaintext = AESGCM(key).decrypt(
            nonce, payload[header_end + 1 :], ARCHIVE_MAGIC + header_bytes
        )
    except PersonalArchiveError:
        raise
    except (ValueError, KeyError, TypeError, InvalidTag, json.JSONDecodeError) as exc:
        raise PersonalArchiveError(
            "口令错误或档案已损坏", code="ARCHIVE_AUTHENTICATION_FAILED"
        ) from exc
    return _read_validated_zip(plaintext)


def load_profile(payload: bytes, passphrase: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    files = decrypt_archive(payload, passphrase)
    try:
        profile = json.loads(files["domain/profile.json"])
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        raise PersonalArchiveError(
            "档案领域数据无效", code="ARCHIVE_DOMAIN_INVALID"
        ) from exc
    if not isinstance(profile, dict):
        raise PersonalArchiveError(
            "档案领域数据无效", code="ARCHIVE_DOMAIN_INVALID"
        )
    objects = {
        name.removeprefix("objects/"): value
        for name, value in files.items()
        if name.startswith("objects/")
    }
    if any(_looks_like_image(value) for value in objects.values()):
        raise PersonalArchiveError(
            "个人档案不得包含图片对象", code="ARCHIVE_IMAGE_FORBIDDEN"
        )
    _validate_profile_pit(profile)
    return profile, objects


def write_private_archive(
    user_id: str,
    archive_id: str,
    name: str,
    payload: bytes,
    settings: Settings | None = None,
) -> str:
    path = private_archive_target_path(user_id, archive_id, name, settings)
    path.write_bytes(payload)
    return path.as_uri()


def private_archive_target_path(
    user_id: str,
    archive_id: str,
    name: str,
    settings: Settings | None = None,
) -> Path:
    root = (settings or get_settings()).private_object_root.resolve()
    directory = (root / user_id / "archives" / archive_id).resolve()
    if not directory.is_relative_to(root):
        raise PersonalArchiveError("档案存储路径无效", code="ARCHIVE_STORAGE_ERROR")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if path.parent != directory or path.name != name:
        raise PersonalArchiveError("档案文件名无效", code="ARCHIVE_STORAGE_ERROR")
    return path


def read_private_archive(uri: str, settings: Settings | None = None) -> bytes:
    return private_archive_path(uri, settings).read_bytes()


def private_archive_path(uri: str, settings: Settings | None = None) -> Path:
    root = (settings or get_settings()).private_object_root.resolve()
    if not uri.startswith("file://"):
        raise PersonalArchiveError("档案存储 URI 无效", code="ARCHIVE_STORAGE_ERROR")
    parsed = urlparse(uri)
    raw_path = unquote(parsed.path)
    if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
        raw_path = raw_path[1:]
    path = Path(raw_path).resolve()
    if not path.is_relative_to(root):
        raise PersonalArchiveError("档案存储越界", code="ARCHIVE_STORAGE_ERROR")
    return path


def delete_private_archive(uri: str | None, settings: Settings | None = None) -> None:
    if not uri:
        return
    path = private_archive_path(uri, settings)
    if path.exists():
        path.unlink()


def delete_imported_archive_objects(
    user_id: str, archive_id: str, settings: Settings | None = None
) -> None:
    root = (settings or get_settings()).private_object_root.resolve()
    directory = (root / user_id / "imported" / archive_id).resolve()
    if not directory.is_relative_to(root):
        raise PersonalArchiveError("导入对象路径越界", code="ARCHIVE_STORAGE_ERROR")
    if directory.exists():
        shutil.rmtree(directory)


def preview_profile(session: Session, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    imported_assets = profile.get("asset_state") or {}
    current = session.get(UserAssetState, user_id)
    current_watchlist = list(current.watchlist) if current is not None else []
    imported_watchlist = list(imported_assets.get("watchlist") or [])
    current_positions = {
        str(item.get("symbol")): item for item in (current.positions if current is not None else [])
    }
    imported_positions = {
        str(item.get("symbol")): item for item in imported_assets.get("positions") or []
    }
    position_conflicts = [
        {
            "symbol": symbol,
            "current": current_positions[symbol],
            "imported": value,
            "default": "CURRENT",
        }
        for symbol, value in imported_positions.items()
        if symbol in current_positions
        and stable_hash(current_positions[symbol]) != stable_hash(value)
    ]
    imported_thread_ids = {
        str(item.get("thread_id")) for item in profile.get("ai_chat_threads") or []
    }
    duplicate_threads = 0
    if imported_thread_ids:
        duplicate_threads = len(
            session.scalars(
                select(AIChatThread.thread_id).where(
                    AIChatThread.user_id == user_id,
                    AIChatThread.thread_id.in_(imported_thread_ids),
                )
            ).all()
        )
    return {
        "watchlist": {
            "new": [item for item in imported_watchlist if item not in current_watchlist],
            "duplicate": [item for item in imported_watchlist if item in current_watchlist],
            "rule": "UNION_CURRENT_ORDER",
        },
        "positions": {
            "new": [item for item in imported_positions if item not in current_positions],
            "conflicts": position_conflicts,
        },
        "total_assets": {
            "current": (
                str(current.total_assets)
                if current and current.total_assets is not None
                else None
            ),
            "imported": imported_assets.get("total_assets"),
            "default": "CURRENT",
        },
        "research_preference": {
            "current": _row_dict(session.get(UserResearchPreference, user_id)),
            "imported": profile.get("research_preference"),
            "default": "CURRENT",
        },
        "automatic_research_reports": {
            "current": [
                _row_dict(row)
                for row in session.scalars(
                    select(AutomaticResearchReportConfig).where(
                        AutomaticResearchReportConfig.user_id == user_id
                    )
                ).all()
            ],
            "imported": profile.get("automatic_research_reports") or [],
            "default": "CURRENT",
        },
        "history": {
            "threads": len(profile.get("ai_chat_threads") or []),
            "thread_id_duplicates": duplicate_threads,
            "runs": len(profile.get("job_runs") or []),
            "reports": len(profile.get("reports") or []),
            "backtests": len(profile.get("backtests") or []),
            "classification": _preview_history_classification(
                session, user_id, profile
            ),
        },
        "images": "IMAGE_NOT_EXPORTED",
    }


def _preview_history_classification(
    session: Session, user_id: str, profile: dict[str, Any]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    specs: tuple[tuple[str, Any, str, set[str]], ...] = (
        ("chat_threads", AIChatThread, "thread_id", {"user_id"}),
        (
            "chat_messages",
            AIChatMessage,
            "message_id",
            {
                "thread_id",
                "parent_message_id",
                "idempotency_key_sha256",
                "request_sha256",
                "attachment_ids",
                "attachments",
                "private_context_snapshot",
            },
        ),
        (
            "research_runs",
            JobRun,
            "run_id",
            {"user_id", "idempotency_key", "active_research_key"},
        ),
        ("reports", ReportRow, "report_id", {"object_uri"}),
        ("backtests", BacktestRun, "backtest_id", {"user_id", "input_hash"}),
    )
    source_names = {
        "chat_threads": "ai_chat_threads",
        "chat_messages": "ai_chat_messages",
        "research_runs": "job_runs",
        "reports": "reports",
        "backtests": "backtests",
    }
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for label, model, pk, ignored in specs:
        buckets: dict[str, list[dict[str, Any]]] = {
            "new": [],
            "duplicate": [],
            "conflict": [],
        }
        for raw in profile.get(source_names[label]) or []:
            if not isinstance(raw, dict):
                continue
            source_id = str(raw.get(pk) or "")
            existing = session.get(model, source_id) if source_id else None
            entry = {
                "source_id": source_id,
                "imported_hash": _normalized_record_hash(raw, ignored=ignored),
            }
            if existing is None:
                buckets["new"].append(entry)
                continue
            owner_id = _history_owner_id(session, existing)
            entry["current_hash"] = _normalized_record_hash(
                existing, ignored=ignored
            )
            if owner_id == user_id and _record_equivalent(
                existing, raw, ignored=ignored
            ):
                buckets["duplicate"].append(entry)
            else:
                buckets["conflict"].append(entry)
        result[label] = buckets
    return result


def _history_owner_id(session: Session, row: Any) -> str | None:
    if isinstance(row, (AIChatThread, BacktestRun, JobRun)):
        return row.user_id
    if isinstance(row, AIChatMessage):
        thread = session.get(AIChatThread, row.thread_id)
        return thread.user_id if thread is not None else None
    if isinstance(row, ReportRow):
        run = session.get(JobRun, row.run_id)
        return run.user_id if run is not None else None
    return None


def apply_profile(
    session: Session,
    *,
    user_id: str,
    profile: dict[str, Any],
    objects: dict[str, bytes],
    merge_options: dict[str, Any],
    archive_id: str,
) -> dict[str, Any]:
    counts = {"inserted": 0, "skipped": 0, "remapped": 0}
    _merge_assets(session, user_id, profile.get("asset_state") or {}, merge_options)
    _merge_preference(
        session,
        user_id,
        profile.get("research_preference"),
        profile.get("automatic_research_reports"),
        merge_options,
    )
    thread_map = _import_threads(session, user_id, profile, counts)
    run_map = _import_runs(session, user_id, profile, counts)
    snapshot_map = _import_snapshots(
        session, user_id, profile, run_map, objects, archive_id, counts
    )
    report_map = _import_run_dependents(
        session, user_id, profile, run_map, snapshot_map, objects, archive_id, counts
    )
    _import_user_history(
        session,
        user_id,
        profile,
        run_map,
        snapshot_map,
        report_map,
        objects,
        archive_id,
        counts,
    )
    return {**counts, "thread_mappings": len(thread_map), "run_mappings": len(run_map)}


def _collect_profile(
    session: Session, user_id: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    assets = session.get(UserAssetState, user_id)
    preference = session.get(UserResearchPreference, user_id)
    automatic_reports = list(
        session.scalars(
            select(AutomaticResearchReportConfig).where(
                AutomaticResearchReportConfig.user_id == user_id
            )
        ).all()
    )
    threads = list(
        session.scalars(select(AIChatThread).where(AIChatThread.user_id == user_id)).all()
    )
    thread_ids = [row.thread_id for row in threads]
    messages = (
        list(
            session.scalars(
                select(AIChatMessage).where(AIChatMessage.thread_id.in_(thread_ids))
            ).all()
        )
        if thread_ids
        else []
    )
    runs = list(session.scalars(select(JobRun).where(JobRun.user_id == user_id)).all())
    run_ids = [row.run_id for row in runs]
    direct: dict[str, list[Any]] = {
        "agent_calls": _rows_for_runs(session, AgentCall, run_ids),
        "evidence": _rows_for_runs(session, EvidenceRow, run_ids),
        "scores": _rows_for_runs(session, ScoreRow, run_ids),
        "candidates": _rows_for_runs(session, CandidateRow, run_ids),
        "portfolios": _rows_for_runs(session, PortfolioRow, run_ids),
        "reports": _rows_for_runs(session, ReportRow, run_ids),
        "audit_events": _rows_for_runs(session, AuditEvent, run_ids),
    }
    backtests = list(
        session.scalars(select(BacktestRun).where(BacktestRun.user_id == user_id)).all()
    )
    trade_plans = list(
        session.scalars(select(TradePlanRow).where(TradePlanRow.user_id == user_id)).all()
    )
    exit_advice = list(
        session.scalars(select(ExitAdviceRow).where(ExitAdviceRow.user_id == user_id)).all()
    )
    snapshot_ids = {
        snapshot_id for row in backtests for snapshot_id in (row.snapshot_ids or [])
    } | {
        snapshot_id for row in trade_plans for snapshot_id in (row.snapshot_ids or [])
    } | {
        row.feature_snapshot_id for row in direct["scores"] if row.feature_snapshot_id
    }
    snapshots = (
        list(
            session.scalars(
                select(SnapshotManifestRow).where(
                    SnapshotManifestRow.snapshot_id.in_(snapshot_ids)
                )
            ).all()
        )
        if snapshot_ids
        else []
    )

    object_payloads: dict[str, bytes] = {}
    object_refs: dict[str, str] = {}
    excluded_image_records: set[str] = set()
    for row in direct["reports"]:
        payload = _read_domain_object(row.object_uri)
        if payload is not None and _looks_like_image(payload):
            excluded_image_records.add(f"report:{row.report_id}")
        elif payload is not None:
            digest = sha256_bytes(payload)
            if digest == row.content_sha256:
                object_payloads[digest] = payload
                object_refs[f"report:{row.report_id}"] = digest
    for row in direct["evidence"]:
        if not row.object_uri:
            continue
        payload = _read_domain_object(row.object_uri)
        if payload is not None and _looks_like_image(payload):
            excluded_image_records.add(f"evidence:{row.id}")
        elif payload is not None:
            digest = sha256_bytes(payload)
            if digest == row.payload_sha256:
                object_payloads[digest] = payload
                object_refs[f"evidence:{row.id}"] = digest
    for row in trade_plans:
        if row.object_uri and row.object_sha256:
            payload = _read_domain_object(row.object_uri)
            if payload is not None and _looks_like_image(payload):
                excluded_image_records.add(f"trade_plan:{row.plan_id}")
            elif payload is not None:
                digest = sha256_bytes(payload)
                if digest == row.object_sha256:
                    object_payloads[digest] = payload
                    object_refs[f"trade_plan:{row.plan_id}"] = digest
    for row in snapshots:
        payload = _read_domain_object(row.parquet_uri)
        if payload is not None and _looks_like_image(payload):
            excluded_image_records.add(f"snapshot:{row.snapshot_id}")
        elif payload is not None:
            digest = sha256_bytes(payload)
            if digest == row.payload_sha256:
                object_payloads[digest] = payload
                object_refs[f"snapshot:{row.snapshot_id}"] = digest

    serialized_messages: list[dict[str, Any]] = []
    for row in messages:
        sanitized_message = _sanitize_paths(_row_dict(row) or {})
        item = sanitized_message if isinstance(sanitized_message, dict) else {}
        item["content"] = _strip_image_data_urls(str(item.get("content") or ""))
        # The normalized prompt snapshot is an internal cache-replay artifact,
        # never user-visible conversation content and never exportable data.
        item.pop("private_context_snapshot", None)
        attachment_count = len(item.pop("attachment_ids", []) or [])
        item.pop("idempotency_key_sha256", None)
        item.pop("request_sha256", None)
        if attachment_count:
            item["attachments"] = [
                {"type": "IMAGE_NOT_EXPORTED"} for _ in range(attachment_count)
            ]
        serialized_messages.append(item)

    profile: dict[str, Any] = {
        "schema_version": ARCHIVE_VERSION,
        "asset_state": _without_owner(_row_dict(assets)),
        "research_preference": _without_owner(_row_dict(preference)),
        "automatic_research_reports": [
            _without_owner(_row_dict(row)) for row in automatic_reports
        ],
        "ai_chat_threads": [_without_owner(_row_dict(row)) for row in threads],
        "ai_chat_messages": serialized_messages,
        "job_runs": [_without_owner(_sanitize_paths(_row_dict(row))) for row in runs],
        **{
            name: [
                _sanitize_export_record(name, _row_dict(row), excluded_image_records)
                for row in rows
            ]
            for name, rows in direct.items()
        },
        "backtests": [_without_owner(_sanitize_paths(_row_dict(row))) for row in backtests],
        "trade_plans": [
            _without_owner(
                _sanitize_export_record("trade_plans", _row_dict(row), excluded_image_records)
            )
            for row in trade_plans
        ],
        "exit_advice": [_without_owner(_row_dict(row)) for row in exit_advice],
        "snapshots": [
            _sanitize_export_record("snapshots", _row_dict(row), excluded_image_records)
            for row in snapshots
        ],
        "object_refs": object_refs,
        "image_policy": "IMAGE_NOT_EXPORTED",
    }
    return profile, object_payloads


def _rows_for_runs(session: Session, model: Any, run_ids: list[str]) -> list[Any]:
    if not run_ids:
        return []
    return list(session.scalars(select(model).where(model.run_id.in_(run_ids))).all())


def _row_dict(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    values: dict[str, Any] = {}
    for column in sqlalchemy_inspect(type(row)).columns:
        value = getattr(row, column.key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = format(value, "f")
        values[column.key] = value
    return values


def _without_owner(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    result = dict(value)
    result.pop("user_id", None)
    return result


def _sanitize_paths(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_paths(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized_key = str(key).casefold()
        looks_internal = isinstance(item, str) and (
            item.startswith(("file://", "s3://", "/", "\\"))
            or bool(re.match(r"^[a-zA-Z]:[\\/]", item))
        )
        if (
            isinstance(item, str) and normalized_key.endswith(("path", "uri"))
        ) or (looks_internal and normalized_key.endswith("url")):
            result[key] = None
        else:
            result[key] = _sanitize_paths(item)
    return result


def _strip_image_data_urls(value: str) -> str:
    return re.sub(
        r"data:image/[a-zA-Z0-9.+-]+;base64,[a-zA-Z0-9+/=_-]+",
        "[IMAGE_NOT_EXPORTED]",
        value,
        flags=re.IGNORECASE,
    )


def _sanitize_export_record(
    category: str,
    value: dict[str, Any] | None,
    excluded_images: set[str],
) -> dict[str, Any] | None:
    if value is None:
        return None
    sanitized = _sanitize_paths(value)
    if not isinstance(sanitized, dict):
        raise PersonalArchiveError(
            "档案记录无效", code="ARCHIVE_DOMAIN_INVALID"
        )
    result: dict[str, Any] = sanitized
    if category == "reports" and f"report:{result.get('report_id')}" in excluded_images:
        result["content_sha256"] = None
    elif (
        category == "trade_plans"
        and f"trade_plan:{result.get('plan_id')}" in excluded_images
    ):
        result["object_sha256"] = None
    elif category == "snapshots" and f"snapshot:{result.get('snapshot_id')}" in excluded_images:
        result["payload_sha256"] = None
    return result


def _read_domain_object(uri: str) -> bytes | None:
    try:
        settings = get_settings()
        if uri.startswith("s3://"):
            store = S3ObjectStore(
                bucket=settings.object_store_bucket,
                endpoint_url=settings.object_store_endpoint,
                access_key=settings.object_store_access_key,
                secret_key=settings.object_store_secret_key,
                secure=settings.object_store_secure,
            )
            return store.get(uri)
        parsed = urlparse(uri)
        raw_path = unquote(parsed.path) if uri.startswith("file://") else uri
        if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        path = Path(raw_path).resolve(strict=True)
        allowed_root = settings.lake_root.parent.resolve()
        if not path.is_relative_to(allowed_root):
            return None
        return path.read_bytes()
    except (OSError, ValueError):
        return None


def _looks_like_image(payload: bytes) -> bool:
    head = payload[:4096]
    stripped = head.lstrip(b"\x00\t\r\n \xef\xbb\xbf")
    lower = stripped.lower()
    raster_signatures = (
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"BM",
        b"II*\x00",
        b"MM\x00*",
        b"\x00\x00\x01\x00",
        b"\x00\x00\x02\x00",
        b"8BPS",
        b"\xff\x0a",
        b"\x00\x00\x00\x0cJXL \r\n\x87\n",
    )
    if stripped.startswith(raster_signatures):
        return True
    strong_signatures = raster_signatures[:4]
    if any(0 <= head.find(signature) <= 64 for signature in strong_signatures):
        return True
    if len(stripped) >= 12 and stripped[:4] == b"RIFF" and stripped[8:12] == b"WEBP":
        return True
    if b"<svg" in lower[:2048]:
        return True
    if len(stripped) >= 12 and stripped[4:8] == b"ftyp":
        brand = stripped[8:16].lower()
        if any(item in brand for item in (b"avif", b"avis", b"heic", b"heix", b"mif1")):
            return True
    return False


def _alembic_revision(session: Session) -> str:
    try:
        return str(session.execute(text("SELECT version_num FROM alembic_version")).scalar() or "")
    except Exception:
        return "UNSTAMPED"


def _derive_archive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=32768, r=8, p=1).derive(
        passphrase.encode("utf-8")
    )


def _server_keys(settings: Settings) -> list[tuple[str, bytes]]:
    configured = (
        settings.personal_data_encryption_keys
        or settings.model_settings_encryption_keys
        or ""
    )
    keys: list[tuple[str, bytes]] = []
    for encoded in configured.split(","):
        value = encoded.strip()
        if not value:
            continue
        try:
            raw = base64.urlsafe_b64decode(value.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise PersonalArchiveError(
                "个人数据加密密钥无效", code="DATA_ENCRYPTION_NOT_CONFIGURED"
            ) from exc
        if len(raw) != 32:
            raise PersonalArchiveError(
                "个人数据加密密钥无效", code="DATA_ENCRYPTION_NOT_CONFIGURED"
            )
        keys.append((sha256_bytes(raw)[:16], raw))
    if not keys:
        raise PersonalArchiveError(
            "个人数据加密尚未配置", code="DATA_ENCRYPTION_NOT_CONFIGURED"
        )
    return keys


def _validate_profile_pit(profile: dict[str, Any]) -> None:
    runs = {
        str(item.get("run_id")): item
        for item in profile.get("job_runs") or []
        if isinstance(item, dict) and item.get("run_id")
    }
    for name in ("scores", "candidates"):
        for item in profile.get(name) or []:
            if not isinstance(item, dict):
                raise PersonalArchiveError(
                    "档案领域记录无效", code="ARCHIVE_DOMAIN_INVALID"
                )
            run = runs.get(str(item.get("run_id")))
            if run is None:
                raise PersonalArchiveError(
                    "档案记录引用无效运行", code="ARCHIVE_REFERENCE_INVALID"
                )
            try:
                item_decision = datetime.fromisoformat(str(item["decision_at"]))
                run_decision = datetime.fromisoformat(str(run["decision_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise PersonalArchiveError(
                    "档案决策时点无效", code="ARCHIVE_PIT_INVALID"
                ) from exc
            if item_decision != run_decision:
                raise PersonalArchiveError(
                    "档案决策时点不一致", code="ARCHIVE_PIT_INVALID"
                )
    for item in profile.get("portfolios") or []:
        if not isinstance(item, dict) or str(item.get("run_id")) not in runs:
            raise PersonalArchiveError(
                "档案组合引用无效运行", code="ARCHIVE_REFERENCE_INVALID"
            )
    for item in profile.get("evidence") or []:
        if not isinstance(item, dict):
            raise PersonalArchiveError("档案证据无效", code="ARCHIVE_DOMAIN_INVALID")
        run = runs.get(str(item.get("run_id")))
        if run is None:
            raise PersonalArchiveError(
                "档案证据引用无效运行", code="ARCHIVE_REFERENCE_INVALID"
            )
        try:
            available_at = datetime.fromisoformat(str(item["available_at"]))
            decision_at = datetime.fromisoformat(str(run["decision_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PersonalArchiveError(
                "档案证据时点无效", code="ARCHIVE_PIT_INVALID"
            ) from exc
        if available_at > decision_at:
            raise PersonalArchiveError(
                "档案包含未来信息", code="ARCHIVE_PIT_INVALID"
            )


def _write_imported_object(
    user_id: str,
    archive_id: str,
    category: str,
    object_id: str,
    payload: bytes,
) -> str:
    if _looks_like_image(payload):
        raise PersonalArchiveError(
            "个人档案不得包含图片对象", code="ARCHIVE_IMAGE_FORBIDDEN"
        )
    root = get_settings().private_object_root.resolve()
    directory = (root / user_id / "imported" / archive_id / category).resolve()
    if not directory.is_relative_to(root):
        raise PersonalArchiveError("导入对象路径越界", code="ARCHIVE_STORAGE_ERROR")
    directory.mkdir(parents=True, exist_ok=True)
    safe_name = stable_hash({"id": object_id, "sha256": sha256_bytes(payload)})
    path = (directory / safe_name).resolve()
    if path.parent != directory:
        raise PersonalArchiveError("导入对象路径无效", code="ARCHIVE_STORAGE_ERROR")
    path.write_bytes(payload)
    return path.as_uri()


def _read_validated_zip(payload: bytes) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload), "r")
    except zipfile.BadZipFile as exc:
        raise PersonalArchiveError("档案 ZIP 已损坏", code="ARCHIVE_ZIP_INVALID") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_FILES:
            raise PersonalArchiveError("档案文件数过多", code="ARCHIVE_LIMIT_EXCEEDED")
        names: set[str] = set()
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if (
                info.filename in names
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in info.filename
            ):
                raise PersonalArchiveError(
                    "档案包含不安全路径", code="ARCHIVE_PATH_UNSAFE"
                )
            names.add(info.filename)
            total += info.file_size
            if total > MAX_UNPACKED_BYTES:
                raise PersonalArchiveError(
                    "档案解压数据过大", code="ARCHIVE_LIMIT_EXCEEDED"
                )
        try:
            manifest = json.loads(archive.read("manifest.json"))
            entries = manifest["files"]
            if (
                manifest.get("format") != ARCHIVE_FORMAT
                or int(manifest.get("format_version", 0)) != ARCHIVE_VERSION
                or not isinstance(entries, dict)
            ):
                raise PersonalArchiveError(
                    "档案版本不兼容", code="ARCHIVE_VERSION_UNSUPPORTED"
                )
            auth = manifest.get("source_auth")
            if not isinstance(auth, dict) or auth.get("algorithm") != "HMAC-SHA256":
                raise PersonalArchiveError(
                    "档案来源无法验证", code="ARCHIVE_SOURCE_UNTRUSTED"
                )
            key_id = str(auth.get("key_id") or "")
            supplied_tag = str(auth.get("tag") or "")
            authenticated_manifest = dict(manifest)
            authenticated_manifest.pop("source_auth", None)
            signing_key = next(
                (
                    key
                    for candidate_id, key in _server_keys(get_settings())
                    if candidate_id == key_id
                ),
                None,
            )
            expected_tag = (
                hmac.new(
                    signing_key,
                    canonical_json(authenticated_manifest),
                    hashlib.sha256,
                ).hexdigest()
                if signing_key is not None
                else ""
            )
            if not supplied_tag or not hmac.compare_digest(supplied_tag, expected_tag):
                raise PersonalArchiveError(
                    "档案来源无法验证", code="ARCHIVE_SOURCE_UNTRUSTED"
                )
        except KeyError as exc:
            raise PersonalArchiveError(
                "档案 manifest 缺失", code="ARCHIVE_MANIFEST_INVALID"
            ) from exc
        expected_names = set(entries) | {"manifest.json"}
        if names != expected_names:
            raise PersonalArchiveError(
                "档案 manifest 与内容不一致", code="ARCHIVE_MANIFEST_INVALID"
            )
        files: dict[str, bytes] = {}
        for name, expected in entries.items():
            data = archive.read(name)
            if (
                sha256_bytes(data) != expected.get("sha256")
                or len(data) != int(expected.get("byte_size", -1))
            ):
                raise PersonalArchiveError(
                    "档案文件哈希不匹配", code="ARCHIVE_HASH_MISMATCH"
                )
            files[name] = data
        return files


def _merge_assets(
    session: Session, user_id: str, imported: dict[str, Any], options: dict[str, Any]
) -> None:
    if not imported:
        return
    row = session.get(UserAssetState, user_id)
    now = datetime.now(UTC)
    if row is None:
        row = UserAssetState(user_id=user_id, updated_at=now)
        session.add(row)
        current_watchlist: list[str] = []
        current_positions: list[dict[str, Any]] = []
    else:
        current_watchlist = list(row.watchlist)
        current_positions = [dict(item) for item in row.positions]
    row.watchlist = current_watchlist + [
        item for item in imported.get("watchlist") or [] if item not in current_watchlist
    ]
    imported_positions = {
        str(item.get("symbol")): dict(item) for item in imported.get("positions") or []
    }
    raw_selections = options.get("positions")
    selections: dict[str, Any] = raw_selections if isinstance(raw_selections, dict) else {}
    merged: list[dict[str, Any]] = []
    current_symbols: set[str] = set()
    for item in current_positions:
        symbol = str(item.get("symbol"))
        current_symbols.add(symbol)
        if selections.get(symbol) == "IMPORTED" and symbol in imported_positions:
            merged.append(imported_positions[symbol])
        else:
            merged.append(item)
    merged.extend(
        item for symbol, item in imported_positions.items() if symbol not in current_symbols
    )
    row.positions = merged
    if options.get("total_assets") == "IMPORTED":
        value = imported.get("total_assets")
        row.total_assets = Decimal(str(value)) if value is not None else None
    if options.get("exit_monitor") == "IMPORTED":
        row.exit_monitor_enabled = bool(imported.get("exit_monitor_enabled", False))
        trigger = imported.get("default_profit_trigger")
        row.default_profit_trigger = Decimal(str(trigger)) if trigger is not None else None
    row.updated_at = now


def _merge_preference(
    session: Session,
    user_id: str,
    imported: Any,
    imported_reports: Any,
    options: dict[str, Any],
) -> None:
    if not isinstance(imported, dict):
        return
    now = datetime.now(UTC)
    row = session.get(UserResearchPreference, user_id)
    if row is None:
        row = UserResearchPreference(
            user_id=user_id,
            auto_enabled=bool(imported.get("auto_enabled", False)),
            updated_at=now,
        )
        session.add(row)
    elif options.get("research_preference") == "IMPORTED":
        row.auto_enabled = bool(imported.get("auto_enabled", False))
        row.updated_at = now
    reports = imported_reports if isinstance(imported_reports, list) else []
    use_imported = options.get("research_preference") == "IMPORTED"
    for raw in reports:
        if not isinstance(raw, dict) or raw.get("slot") not in {"A", "B"}:
            continue
        key = {"user_id": user_id, "slot": str(raw["slot"])}
        config = session.get(AutomaticResearchReportConfig, key)
        if config is not None and not use_imported:
            continue
        if config is None:
            config = AutomaticResearchReportConfig(
                **key,
                total_budget=Decimal(str(raw.get("total_budget", "1000000"))),
                per_symbol_budget=Decimal(str(raw.get("per_symbol_budget", "80000"))),
                updated_at=now,
            )
            session.add(config)
        config.enabled = bool(raw.get("enabled", False))
        config.scope = str(raw.get("scope", "MARKET"))
        config.symbols = list(raw.get("symbols") or [])
        config.total_budget = Decimal(str(raw.get("total_budget", "1000000")))
        config.per_symbol_budget = Decimal(str(raw.get("per_symbol_budget", "80000")))
        maximum = raw.get("max_stock_price")
        config.max_stock_price = Decimal(str(maximum)) if maximum is not None else None
        config.config_version = max(1, int(raw.get("config_version", 1)))
        config.updated_at = now
    # Old v1 archives only contain the legacy switch. Materialize any missing
    # slots so reads and the normalized scheduler observe the same state.
    session.flush()
    stored_slots = set(
        session.scalars(
            select(AutomaticResearchReportConfig.slot).where(
                AutomaticResearchReportConfig.user_id == user_id
            )
        ).all()
    )
    for slot in {"A", "B"} - stored_slots:
        session.add(
            AutomaticResearchReportConfig(
                user_id=user_id,
                slot=slot,
                enabled=bool(row.auto_enabled and slot == "A"),
                scope="MARKET",
                symbols=[],
                total_budget=Decimal("1000000"),
                per_symbol_budget=Decimal("80000"),
                updated_at=now,
            )
        )
    session.flush()
    # The normalized A/B rows are authoritative. Keep the legacy preference
    # switch synchronized with the final merged state even when CURRENT keeps
    # an existing slot and IMPORTED only fills a missing one.
    row.auto_enabled = bool(
        session.scalar(
            select(AutomaticResearchReportConfig.user_id)
            .where(
                AutomaticResearchReportConfig.user_id == user_id,
                AutomaticResearchReportConfig.enabled.is_(True),
            )
            .limit(1)
        )
    )
    row.updated_at = now


def _import_threads(
    session: Session, user_id: str, profile: dict[str, Any], counts: dict[str, int]
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in profile.get("ai_chat_threads") or []:
        source_id = str(raw.get("thread_id"))
        existing = session.get(AIChatThread, source_id)
        target_id = source_id
        if existing is not None:
            comparable = _row_dict(existing) or {}
            comparable.pop("user_id", None)
            if existing.user_id == user_id and stable_hash(comparable) == stable_hash(raw):
                mapping[source_id] = source_id
                counts["skipped"] += 1
                continue
            target_id = _import_id(user_id, "chat-thread", source_id)
            mapped = session.get(AIChatThread, target_id)
            if mapped is not None:
                if mapped.user_id != user_id or not _record_equivalent(
                    mapped, raw, ignored={"user_id", "thread_id"}
                ):
                    raise PersonalArchiveError(
                        "聊天线程重映射冲突", code="ARCHIVE_ID_CONFLICT"
                    )
                mapping[source_id] = target_id
                counts["skipped"] += 1
                continue
            counts["remapped"] += 1
        values = _model_values(AIChatThread, raw)
        values.update(thread_id=target_id, user_id=user_id)
        session.add(AIChatThread(**values))
        mapping[source_id] = target_id
        counts["inserted"] += 1
    session.flush()
    message_mapping: dict[str, str] = {}
    skipped_messages: set[str] = set()
    pending = list(profile.get("ai_chat_messages") or [])
    for raw in pending:
        source_id = str(raw.get("message_id"))
        existing_message = session.get(AIChatMessage, source_id)
        if (
            existing_message is not None
            and existing_message.thread_id == mapping.get(str(raw.get("thread_id")))
            and _record_equivalent(
                existing_message,
                raw,
                ignored={
                    "message_id",
                    "thread_id",
                    "parent_message_id",
                    "idempotency_key_sha256",
                    "request_sha256",
                    "attachment_ids",
                    "attachments",
                    "private_context_snapshot",
                },
            )
        ):
            message_mapping[source_id] = source_id
            skipped_messages.add(source_id)
            counts["skipped"] += 1
            continue
        target_id = (
            source_id
            if existing_message is None
            else _import_id(user_id, "chat-message", source_id)
        )
        mapped_message = session.get(AIChatMessage, target_id)
        if mapped_message is not None:
            expected_thread = mapping.get(str(raw.get("thread_id")))
            if mapped_message.thread_id != expected_thread or not _record_equivalent(
                mapped_message,
                raw,
                ignored={
                    "message_id",
                    "thread_id",
                    "parent_message_id",
                    "idempotency_key_sha256",
                    "request_sha256",
                    "attachment_ids",
                    "attachments",
                    "private_context_snapshot",
                },
            ):
                raise PersonalArchiveError(
                    "聊天消息重映射冲突", code="ARCHIVE_ID_CONFLICT"
                )
            message_mapping[source_id] = target_id
            skipped_messages.add(source_id)
            counts["skipped"] += 1
            continue
        if target_id != source_id:
            counts["remapped"] += 1
        message_mapping[source_id] = target_id
    for raw in pending:
        thread_id = mapping.get(str(raw.get("thread_id")))
        if not thread_id:
            raise PersonalArchiveError(
                "聊天消息引用无效线程", code="ARCHIVE_REFERENCE_INVALID"
            )
        values = _model_values(AIChatMessage, raw)
        source_id = str(raw.get("message_id"))
        if source_id in skipped_messages:
            continue
        values.update(
            message_id=message_mapping[source_id],
            thread_id=thread_id,
            parent_message_id=message_mapping.get(str(raw.get("parent_message_id"))),
            attachment_ids=[],
            idempotency_key_sha256=None,
            request_sha256=None,
        )
        session.add(AIChatMessage(**values))
        counts["inserted"] += 1
    session.flush()
    return mapping


def _import_runs(
    session: Session,
    user_id: str,
    profile: dict[str, Any],
    counts: dict[str, int],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in profile.get("job_runs") or []:
        source_id = str(raw.get("run_id"))
        existing = session.get(JobRun, source_id)
        if (
            existing is not None
            and existing.user_id == user_id
            and _record_equivalent(
                existing,
                raw,
                ignored={"user_id", "idempotency_key", "active_research_key"},
            )
        ):
            mapping[source_id] = source_id
            counts["skipped"] += 1
            continue
        target_id = (
            source_id if existing is None else _import_id(user_id, "job-run", source_id)
        )
        mapped = session.get(JobRun, target_id)
        if mapped is not None:
            if mapped.user_id != user_id or not _record_equivalent(
                mapped,
                raw,
                ignored={
                    "run_id",
                    "user_id",
                    "idempotency_key",
                    "active_research_key",
                },
            ):
                raise PersonalArchiveError(
                    "运行记录重映射冲突", code="ARCHIVE_ID_CONFLICT"
                )
            mapping[source_id] = target_id
            counts["skipped"] += 1
            continue
        values = _model_values(JobRun, raw)
        values.update(
            run_id=target_id,
            user_id=user_id,
            idempotency_key=stable_hash(
                {"import_user": user_id, "source_run_id": source_id}
            ),
            active_research_key=None,
        )
        session.add(JobRun(**values))
        mapping[source_id] = target_id
        counts["inserted"] += 1
        counts["remapped"] += int(target_id != source_id)
    session.flush()
    return mapping


def _import_snapshots(
    session: Session,
    user_id: str,
    profile: dict[str, Any],
    run_map: dict[str, str],
    objects: dict[str, bytes],
    archive_id: str,
    counts: dict[str, int],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    refs = profile.get("object_refs") or {}
    for raw in profile.get("snapshots") or []:
        source_id = str(raw.get("snapshot_id"))
        existing = session.get(SnapshotManifestRow, source_id)
        if existing is not None:
            if existing.payload_sha256 == raw.get("payload_sha256"):
                mapping[source_id] = source_id
                counts["skipped"] += 1
                continue
            target_id = _import_id(user_id, "snapshot", source_id)
            mapped = session.get(SnapshotManifestRow, target_id)
            if mapped is not None:
                if mapped.payload_sha256 != raw.get("payload_sha256"):
                    raise PersonalArchiveError(
                        "快照重映射冲突", code="ARCHIVE_ID_CONFLICT"
                    )
                mapping[source_id] = target_id
                counts["skipped"] += 1
                continue
            counts["remapped"] += 1
        else:
            target_id = source_id
        digest = refs.get(f"snapshot:{source_id}")
        payload = objects.get(str(digest))
        if payload is None or sha256_bytes(payload) != raw.get("payload_sha256"):
            counts["skipped"] += 1
            continue
        uri = _write_imported_object(
            user_id, archive_id, "snapshots", target_id, payload
        )
        values = _model_values(SnapshotManifestRow, raw)
        values.update(
            snapshot_id=target_id,
            run_id=run_map.get(str(raw.get("run_id"))),
            parquet_uri=uri,
        )
        session.add(SnapshotManifestRow(**values))
        mapping[source_id] = target_id
        counts["inserted"] += 1
    session.flush()
    return mapping


def _import_run_dependents(
    session: Session,
    user_id: str,
    profile: dict[str, Any],
    run_map: dict[str, str],
    snapshot_map: dict[str, str],
    objects: dict[str, bytes],
    archive_id: str,
    counts: dict[str, int],
) -> dict[str, str]:
    refs = profile.get("object_refs") or {}
    specs = (
        ("agent_calls", AgentCall, "call_id"),
        ("evidence", EvidenceRow, "id"),
        ("scores", ScoreRow, "score_id"),
        ("candidates", CandidateRow, "candidate_id"),
        ("portfolios", PortfolioRow, "portfolio_id"),
        ("audit_events", AuditEvent, "event_id"),
    )
    for name, model, pk in specs:
        for raw in profile.get(name) or []:
            source_run = str(raw.get("run_id"))
            if source_run not in run_map:
                raise PersonalArchiveError(
                    "历史记录引用无效运行", code="ARCHIVE_REFERENCE_INVALID"
                )
            values = _model_values(model, raw)
            source_id = str(raw.get(pk))
            existing = session.get(model, source_id)
            ignored_fields = {pk, "run_id", "object_uri", "object_occurrence_id"}
            if (
                existing is not None
                and getattr(existing, "run_id", None) == run_map[source_run]
                and _record_equivalent(existing, raw, ignored=ignored_fields)
            ):
                counts["skipped"] += 1
                continue
            target_id = (
                source_id
                if existing is None
                else _import_id(user_id, name, source_id)
            )
            mapped = session.get(model, target_id)
            if mapped is not None:
                if (
                    getattr(mapped, "run_id", None) != run_map[source_run]
                    or not _record_equivalent(mapped, raw, ignored=ignored_fields)
                ):
                    raise PersonalArchiveError(
                        "历史记录重映射冲突", code="ARCHIVE_ID_CONFLICT"
                    )
                counts["skipped"] += 1
                continue
            values.update({pk: target_id, "run_id": run_map[source_run]})
            if model is ScoreRow:
                source_snapshot = str(raw.get("feature_snapshot_id") or "")
                if source_snapshot not in snapshot_map:
                    raise PersonalArchiveError(
                        "评分引用无效快照", code="ARCHIVE_REFERENCE_INVALID"
                    )
                values["feature_snapshot_id"] = snapshot_map[source_snapshot]
            if model is EvidenceRow:
                values["object_occurrence_id"] = None
                digest = refs.get(f"evidence:{source_id}")
                payload = objects.get(str(digest))
                if payload is not None:
                    if sha256_bytes(payload) != raw.get("payload_sha256"):
                        raise PersonalArchiveError(
                            "证据对象哈希不匹配", code="ARCHIVE_HASH_MISMATCH"
                        )
                    values["object_uri"] = _write_imported_object(
                        user_id, archive_id, "evidence", target_id, payload
                    )
                else:
                    values["object_uri"] = None
            session.add(model(**values))
            counts["inserted"] += 1
            counts["remapped"] += int(target_id != source_id)

    report_map: dict[str, str] = {}
    for raw in profile.get("reports") or []:
        source_id = str(raw.get("report_id"))
        source_run = str(raw.get("run_id"))
        if source_run not in run_map:
            raise PersonalArchiveError(
                "报告引用无效运行", code="ARCHIVE_REFERENCE_INVALID"
            )
        digest = refs.get(f"report:{source_id}")
        payload = objects.get(str(digest))
        if payload is None or sha256_bytes(payload) != raw.get("content_sha256"):
            counts["skipped"] += 1
            continue
        existing_report = session.get(ReportRow, source_id)
        if existing_report is not None and (
            existing_report.run_id == run_map[source_run]
            and existing_report.content_sha256 == raw.get("content_sha256")
        ):
            report_map[source_id] = source_id
            counts["skipped"] += 1
            continue
        target_id = (
            source_id
            if existing_report is None
            else _import_id(user_id, "report", source_id)
        )
        mapped_report = session.get(ReportRow, target_id)
        if mapped_report is not None:
            if (
                mapped_report.run_id != run_map[source_run]
                or mapped_report.content_sha256 != raw.get("content_sha256")
            ):
                raise PersonalArchiveError(
                    "报告重映射冲突", code="ARCHIVE_ID_CONFLICT"
                )
            report_map[source_id] = target_id
            counts["skipped"] += 1
            continue
        uri = _write_imported_object(
            user_id, archive_id, "reports", target_id, payload
        )
        values = _model_values(ReportRow, raw)
        values.update(report_id=target_id, run_id=run_map[source_run], object_uri=uri)
        session.add(ReportRow(**values))
        report_map[source_id] = target_id
        counts["inserted"] += 1
        counts["remapped"] += int(target_id != source_id)
    session.flush()
    return report_map


def _import_user_history(
    session: Session,
    user_id: str,
    profile: dict[str, Any],
    run_map: dict[str, str],
    snapshot_map: dict[str, str],
    report_map: dict[str, str],
    objects: dict[str, bytes],
    archive_id: str,
    counts: dict[str, int],
) -> None:
    for raw in profile.get("backtests") or []:
        source_id = str(raw.get("backtest_id"))
        existing = session.get(BacktestRun, source_id)
        if (
            existing is not None
            and existing.user_id == user_id
            and _record_equivalent(
                existing, raw, ignored={"user_id", "input_hash"}
            )
        ):
            counts["skipped"] += 1
            continue
        target_id = (
            source_id
            if existing is None
            else _import_id(user_id, "backtest", source_id)
        )
        mapped = session.get(BacktestRun, target_id)
        if mapped is not None:
            if mapped.user_id != user_id or not _record_equivalent(
                mapped,
                raw,
                ignored={
                    "backtest_id",
                    "user_id",
                    "run_id",
                    "snapshot_ids",
                    "input_hash",
                },
            ):
                raise PersonalArchiveError(
                    "回测重映射冲突", code="ARCHIVE_ID_CONFLICT"
                )
            counts["skipped"] += 1
            continue
        values = _model_values(BacktestRun, raw)
        values.update(
            backtest_id=target_id,
            user_id=user_id,
            run_id=run_map.get(str(raw.get("run_id"))),
            snapshot_ids=[
                snapshot_map[item] for item in raw.get("snapshot_ids") or [] if item in snapshot_map
            ],
            input_hash=stable_hash(
                {"import_user": user_id, "source": raw.get("input_hash")}
            ),
        )
        session.add(BacktestRun(**values))
        counts["inserted"] += 1
        counts["remapped"] += int(target_id != source_id)
    for raw in profile.get("trade_plans") or []:
        source_id = str(raw.get("plan_id"))
        report_id = report_map.get(str(raw.get("report_id")))
        run_id = run_map.get(str(raw.get("run_id")))
        if not report_id or not run_id:
            counts["skipped"] += 1
            continue
        existing_plan = session.get(TradePlanRow, source_id)
        if (
            existing_plan is not None
            and existing_plan.user_id == user_id
            and _record_equivalent(
                existing_plan,
                raw,
                ignored={"user_id", "input_hash", "active_trade_plan_key", "object_uri"},
            )
        ):
            counts["skipped"] += 1
            continue
        target_id = (
            source_id
            if existing_plan is None
            else _import_id(user_id, "trade-plan", source_id)
        )
        mapped_plan = session.get(TradePlanRow, target_id)
        if mapped_plan is not None:
            if mapped_plan.user_id != user_id or not _record_equivalent(
                mapped_plan,
                raw,
                ignored={
                    "plan_id",
                    "user_id",
                    "report_id",
                    "run_id",
                    "snapshot_ids",
                    "input_hash",
                    "active_trade_plan_key",
                    "object_uri",
                },
            ):
                raise PersonalArchiveError(
                    "交易方案重映射冲突", code="ARCHIVE_ID_CONFLICT"
                )
            counts["skipped"] += 1
            continue
        object_uri: str | None = None
        object_sha256: str | None = None
        digest = (profile.get("object_refs") or {}).get(f"trade_plan:{source_id}")
        object_payload = objects.get(str(digest))
        if object_payload is not None:
            if sha256_bytes(object_payload) != raw.get("object_sha256"):
                raise PersonalArchiveError(
                    "交易方案对象哈希不匹配", code="ARCHIVE_HASH_MISMATCH"
                )
            object_uri = _write_imported_object(
                user_id, archive_id, "trade-plans", target_id, object_payload
            )
            object_sha256 = sha256_bytes(object_payload)
        values = _model_values(TradePlanRow, raw)
        values.update(
            plan_id=target_id,
            user_id=user_id,
            report_id=report_id,
            run_id=run_id,
            snapshot_ids=[
                snapshot_map[item] for item in raw.get("snapshot_ids") or [] if item in snapshot_map
            ],
            active_trade_plan_key=None,
            object_uri=object_uri,
            object_sha256=object_sha256,
            input_hash=stable_hash(
                {"import_user": user_id, "source": raw.get("input_hash")}
            ),
        )
        session.add(TradePlanRow(**values))
        counts["inserted"] += 1
        counts["remapped"] += int(target_id != source_id)
    for raw in profile.get("exit_advice") or []:
        source_id = str(raw.get("advice_id"))
        existing_advice = session.get(ExitAdviceRow, source_id)
        if (
            existing_advice is not None
            and existing_advice.user_id == user_id
            and _record_equivalent(
                existing_advice, raw, ignored={"user_id", "input_hash"}
            )
        ):
            counts["skipped"] += 1
            continue
        target_id = (
            source_id
            if existing_advice is None
            else _import_id(user_id, "exit-advice", source_id)
        )
        mapped_advice = session.get(ExitAdviceRow, target_id)
        if mapped_advice is not None:
            if mapped_advice.user_id != user_id or not _record_equivalent(
                mapped_advice,
                raw,
                ignored={"advice_id", "user_id", "input_hash"},
            ):
                raise PersonalArchiveError(
                    "退出建议重映射冲突", code="ARCHIVE_ID_CONFLICT"
                )
            counts["skipped"] += 1
            continue
        values = _model_values(ExitAdviceRow, raw)
        values.update(
            advice_id=target_id,
            user_id=user_id,
            input_hash=stable_hash(
                {"import_user": user_id, "source": raw.get("input_hash")}
            ),
        )
        session.add(ExitAdviceRow(**values))
        counts["inserted"] += 1
        counts["remapped"] += int(target_id != source_id)
    session.flush()


def _model_values(model: Any, raw: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in sqlalchemy_inspect(model).columns:
        if column.key not in raw:
            continue
        value = raw[column.key]
        if value is None:
            result[column.key] = None
        elif column.type.python_type is datetime:
            result[column.key] = datetime.fromisoformat(str(value))
        elif column.type.python_type is date:
            result[column.key] = date.fromisoformat(str(value))
        elif column.type.python_type is Decimal:
            result[column.key] = Decimal(str(value))
        else:
            result[column.key] = value
    return result


def _record_equivalent(
    existing: Any, raw: dict[str, Any], *, ignored: set[str]
) -> bool:
    return _normalized_record_hash(existing, ignored=ignored) == _normalized_record_hash(
        raw, ignored=ignored
    )


def _normalized_record_hash(value: Any, *, ignored: set[str]) -> str:
    current = dict(value) if isinstance(value, dict) else (_row_dict(value) or {})
    for key in ignored:
        current.pop(key, None)
    return stable_hash(_normalize_compare_value(_sanitize_paths(current)))


def _normalize_compare_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_compare_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_compare_value(item) for item in value]
    if isinstance(value, str) and "T" in value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    return value


def _import_id(user_id: str, category: str, source_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"ashare-ai:{user_id}:{category}:{source_id}"))
