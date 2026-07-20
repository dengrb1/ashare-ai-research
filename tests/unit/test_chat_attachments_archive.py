from __future__ import annotations

import hashlib
import hmac
import io
import json
import struct
import zipfile
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ashare_ai.agents.attachments import AttachmentError, AttachmentService, inspect_image
from ashare_ai.agents.chat import _resolve_mentions
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import canonical_json, sha256_bytes
from ashare_ai.storage.models import (
    AIChatMessage,
    AIChatThread,
    Base,
    SecurityMaster,
    UserAccount,
    UserAssetState,
)
from ashare_ai.storage.personal_archive import (
    ARCHIVE_FORMAT,
    PersonalArchiveError,
    _looks_like_image,
    _server_keys,
    _validate_profile_pit,
    apply_profile,
    build_personal_archive,
    decrypt_archive,
    encrypt_archive,
    load_profile,
    preview_profile,
)


def _png(width: int = 2, height: int = 3) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(
        ">II", width, height
    ) + b"\x08\x06\x00\x00\x00" + b"placeholder"


def _database() -> tuple[Session, str]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    user = UserAccount(
        username="archive-user",
        password_hash="not-exported",
        role="ADMIN",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.commit()
    return session, user.user_id


def test_attachment_is_encrypted_user_isolated_and_expires_at_exact_boundary(
    tmp_path,
) -> None:
    session, user_id = _database()
    now = datetime(2026, 7, 20, tzinfo=UTC)
    thread = AIChatThread(
        user_id=user_id,
        title="image",
        created_at=now,
        updated_at=now,
    )
    session.add(thread)
    session.commit()
    settings = Settings(
        _env_file=None,
        private_object_root=tmp_path,
        personal_data_encryption_keys=Fernet.generate_key().decode(),
    )
    service = AttachmentService(session, settings)

    row = service.create(
        user_id=user_id,
        thread_id=thread.thread_id,
        payload=_png(),
        claimed_mime="image/png",
        now=now,
    )

    assert row.expires_at == now + timedelta(days=7)
    assert (
        service.read(
            user_id, row.attachment_id, row.expires_at - timedelta(microseconds=1)
        )
        == _png()
    )
    with pytest.raises(AttachmentError) as caught:
        service.read(user_id, row.attachment_id, row.expires_at)
    assert caught.value.code == "IMAGE_EXPIRED"
    encrypted_path = service._path_from_uri(row.encrypted_object_uri)
    assert _png() not in encrypted_path.read_bytes()
    assert user_id in encrypted_path.parts
    assert service.cleanup_expired(row.expires_at) == 1
    assert not encrypted_path.exists()
    assert row.deletion_reason == "RETENTION_EXPIRED"


def test_image_signature_mime_and_animation_validation() -> None:
    assert inspect_image(_png(), "image/png").width == 2
    with pytest.raises(AttachmentError, match="MIME"):
        inspect_image(_png(), "image/jpeg")
    frame = b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x00"
    animated_gif = b"GIF89a\x01\x00\x01\x00\x00\x00\x00" + frame + frame + b"\x3b"
    with pytest.raises(AttachmentError) as caught:
        inspect_image(animated_gif, "image/gif")
    assert caught.value.code == "ANIMATED_GIF_UNSUPPORTED"
    assert _looks_like_image(b"\xef\xbb\xbf<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    assert _looks_like_image(b"wrapper" + _png())
    assert _looks_like_image(b"BM" + b"\x00" * 30)


def test_personal_archive_excludes_image_metadata_and_authenticates_passphrase() -> None:
    session, user_id = _database()
    now = datetime.now(UTC)
    session.add(
        UserAssetState(
            user_id=user_id,
            watchlist=["600690.SH"],
            positions=[{"symbol": "600690.SH", "name": "海尔智家", "quantity": 100, "cost": 20}],
            updated_at=now,
        )
    )
    thread = AIChatThread(user_id=user_id, title="chat", created_at=now, updated_at=now)
    session.add(thread)
    session.flush()
    session.add(
        AIChatMessage(
            thread_id=thread.thread_id,
            role="user",
            content="analyse",
            status="COMPLETED",
            mentioned_symbols=["600690.SH"],
            mention_refs=[{"symbol": "600690.SH", "name": "海尔智家"}],
            attachment_ids=["secret-image-id"],
            sources=[],
            cache_hit=False,
            created_at=now,
        )
    )
    session.commit()

    payload = build_personal_archive(session, user_id, "correct horse")
    profile, objects = load_profile(payload, "correct horse")

    assert objects == {}
    serialized = json.dumps(profile, ensure_ascii=False)
    assert "secret-image-id" not in serialized
    assert "IMAGE_NOT_EXPORTED" in serialized
    assert "password_hash" not in serialized
    assert "user_accounts" not in serialized
    assert "ADMIN" not in serialized
    with pytest.raises(PersonalArchiveError) as caught:
        load_profile(payload, "wrong passphrase")
    assert caught.value.code == "ARCHIVE_AUTHENTICATION_FAILED"


def test_personal_archive_rejects_unsafe_zip_paths() -> None:
    malicious = b"not executable"
    manifest = {
        "format": ARCHIVE_FORMAT,
        "format_version": 1,
        "files": {
            "../escape": {
                "sha256": sha256_bytes(malicious),
                "byte_size": len(malicious),
            }
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", canonical_json(manifest))
        archive.writestr("../escape", malicious)
    payload = encrypt_archive(buffer.getvalue(), "correct horse")

    with pytest.raises(PersonalArchiveError) as caught:
        decrypt_archive(payload, "correct horse")
    assert caught.value.code == "ARCHIVE_PATH_UNSAFE"


def test_personal_archive_rejects_unsigned_source_and_signed_image_objects() -> None:
    profile_payload = canonical_json({"schema_version": 1})
    files = {"domain/profile.json": profile_payload}
    manifest: dict[str, object] = {
        "format": ARCHIVE_FORMAT,
        "format_version": 1,
        "files": {
            name: {"sha256": sha256_bytes(value), "byte_size": len(value)}
            for name, value in files.items()
        },
    }

    def package(current_manifest: dict[str, object], current_files: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.json", canonical_json(current_manifest))
            for name, value in current_files.items():
                archive.writestr(name, value)
        return encrypt_archive(buffer.getvalue(), "correct horse")

    with pytest.raises(PersonalArchiveError) as caught:
        load_profile(package(manifest, files), "correct horse")
    assert caught.value.code == "ARCHIVE_SOURCE_UNTRUSTED"

    image_files = {**files, "objects/image": _png()}
    signed_manifest: dict[str, object] = {
        "format": ARCHIVE_FORMAT,
        "format_version": 1,
        "files": {
            name: {"sha256": sha256_bytes(value), "byte_size": len(value)}
            for name, value in image_files.items()
        },
    }
    key_id, key = _server_keys(get_settings())[0]
    signed_manifest["source_auth"] = {
        "algorithm": "HMAC-SHA256",
        "key_id": key_id,
        "tag": hmac.new(key, canonical_json(signed_manifest), hashlib.sha256).hexdigest(),
    }
    with pytest.raises(PersonalArchiveError) as caught:
        load_profile(package(signed_manifest, image_files), "correct horse")
    assert caught.value.code == "ARCHIVE_IMAGE_FORBIDDEN"


def test_personal_archive_applies_watchlist_union_and_selected_position_atomically() -> None:
    session, source_user = _database()
    now = datetime.now(UTC)
    target = UserAccount(
        username="target-user",
        password_hash="target-hash",
        role="USER",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(target)
    session.add(
        UserAssetState(
            user_id=source_user,
            watchlist=["600690.SH", "000001.SZ"],
            positions=[
                {"symbol": "600690.SH", "name": "海尔智家", "quantity": 200, "cost": 21}
            ],
            total_assets=500000,
            updated_at=now,
        )
    )
    session.flush()
    source_thread = AIChatThread(
        user_id=source_user,
        title="source chat",
        created_at=now,
        updated_at=now,
    )
    session.add(source_thread)
    session.flush()
    session.add(
        AIChatMessage(
            thread_id=source_thread.thread_id,
            role="user",
            content="source message",
            status="COMPLETED",
            mentioned_symbols=[],
            mention_refs=[],
            attachment_ids=[],
            sources=[],
            cache_hit=False,
            created_at=now,
        )
    )
    session.add(
        UserAssetState(
            user_id=target.user_id,
            watchlist=["000001.SZ"],
            positions=[
                {"symbol": "600690.SH", "name": "海尔智家", "quantity": 100, "cost": 18}
            ],
            total_assets=100000,
            updated_at=now,
        )
    )
    session.commit()
    payload = build_personal_archive(session, source_user, "correct horse")
    profile, objects = load_profile(payload, "correct horse")
    preview = preview_profile(session, target.user_id, profile)
    thread_conflicts = preview["history"]["classification"]["chat_threads"]["conflict"]
    assert [item["source_id"] for item in thread_conflicts] == [source_thread.thread_id]

    result = apply_profile(
        session,
        user_id=target.user_id,
        profile=profile,
        objects=objects,
        merge_options={
            "positions": {"600690.SH": "IMPORTED"},
            "total_assets": "CURRENT",
        },
        archive_id="roundtrip-test",
    )

    imported = session.get(UserAssetState, target.user_id)
    assert imported is not None
    assert imported.watchlist == ["000001.SZ", "600690.SH"]
    assert imported.positions[0]["quantity"] == 200
    assert imported.total_assets == 100000
    target_thread = session.scalar(
        select(AIChatThread).where(AIChatThread.user_id == target.user_id)
    )
    assert target_thread is not None
    assert target_thread.thread_id != source_thread.thread_id
    target_message = session.scalar(
        select(AIChatMessage).where(AIChatMessage.thread_id == target_thread.thread_id)
    )
    assert target_message is not None
    assert target_message.content == "source message"
    assert result["inserted"] == 2
    repeated = apply_profile(
        session,
        user_id=target.user_id,
        profile=profile,
        objects=objects,
        merge_options={},
        archive_id="roundtrip-repeat",
    )
    assert repeated["inserted"] == 0


def test_archive_pit_accepts_portfolio_rows_without_decision_at() -> None:
    decision_at = datetime.now(UTC).isoformat()
    _validate_profile_pit(
        {
            "job_runs": [{"run_id": "run-1", "decision_at": decision_at}],
            "portfolios": [{"portfolio_id": "portfolio-1", "run_id": "run-1"}],
        }
    )


def test_stock_name_mentions_cannot_be_bound_to_a_different_symbol(monkeypatch) -> None:
    session, _ = _database()
    now = datetime.now(UTC)
    for symbol, name, exchange in (
        ("000001.SZ", "平安银行", "SZ"),
        ("600519.SH", "贵州茅台", "SH"),
    ):
        session.add(
            SecurityMaster(
                symbol=symbol,
                trading_date=date(2026, 7, 20),
                exchange=exchange,
                board="MAIN",
                short_name=name,
                list_date=date(1991, 1, 1),
                effective_from=date(2026, 1, 1),
                is_st=False,
                is_suspended=False,
                source="fixture",
                source_record_id=symbol,
                available_at=now,
                fetched_at=now,
                payload_sha256="a" * 64,
                schema_version="v1",
                adapter_version="v1",
                ingestion_run_id="fixture-run",
                availability_basis="DISCLOSED",
            )
        )
    session.commit()
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr("ashare_ai.agents.chat.SessionLocal", factory)

    assert _resolve_mentions(
        "@贵州茅台",
        {},
        [{"symbol": "000001.SZ", "name": "贵州茅台"}],
        now=now,
    ) == []
    assert _resolve_mentions(
        "@贵州茅台",
        {},
        [{"symbol": "600519.SH", "name": "贵州茅台"}],
        now=now,
    ) == [{"symbol": "600519.SH", "name": "贵州茅台"}]
