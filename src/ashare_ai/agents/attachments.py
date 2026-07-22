from __future__ import annotations

import base64
import os
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.storage.models import AIChatAttachment, AIChatThread

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_MESSAGE_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 4
MAX_IMAGE_PIXELS = 40_000_000
IMAGE_RETENTION = timedelta(days=7)


class AttachmentError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    width: int
    height: int


def inspect_image(payload: bytes, claimed_mime: str | None = None) -> ImageInfo:
    if not payload:
        raise AttachmentError("图片内容为空", code="IMAGE_EMPTY")
    if len(payload) > MAX_IMAGE_BYTES:
        raise AttachmentError("单张图片不能超过 10 MB", code="IMAGE_TOO_LARGE")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        info = _png_info(payload)
    elif payload.startswith((b"GIF87a", b"GIF89a")):
        info = _gif_info(payload)
    elif payload.startswith(b"\xff\xd8"):
        info = _jpeg_info(payload)
    elif len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        info = _webp_info(payload)
    else:
        raise AttachmentError("仅支持 PNG、JPEG、WebP 和非动画 GIF", code="IMAGE_TYPE_UNSUPPORTED")
    normalized_claim = (claimed_mime or "").split(";", 1)[0].strip().casefold()
    accepted_claims = {info.mime_type}
    if info.mime_type == "image/jpeg":
        accepted_claims.add("image/jpg")
    if normalized_claim and normalized_claim not in accepted_claims:
        raise AttachmentError("图片 MIME 与文件签名不匹配", code="IMAGE_MIME_MISMATCH")
    if info.width <= 0 or info.height <= 0 or info.width * info.height > MAX_IMAGE_PIXELS:
        raise AttachmentError("图片尺寸无效或过大", code="IMAGE_DIMENSIONS_INVALID")
    return info


def _png_info(payload: bytes) -> ImageInfo:
    if len(payload) < 24 or payload[12:16] != b"IHDR":
        raise AttachmentError("PNG 文件头无效", code="IMAGE_SIGNATURE_INVALID")
    if b"acTL" in payload:
        raise AttachmentError("不支持动画 PNG", code="ANIMATED_PNG_UNSUPPORTED")
    width, height = struct.unpack(">II", payload[16:24])
    return ImageInfo("image/png", width, height)


def _gif_info(payload: bytes) -> ImageInfo:
    if len(payload) < 13:
        raise AttachmentError("GIF 文件头无效", code="IMAGE_SIGNATURE_INVALID")
    frame_count = _gif_frame_count(payload)
    if frame_count != 1:
        code = "ANIMATED_GIF_UNSUPPORTED" if frame_count > 1 else "IMAGE_SIGNATURE_INVALID"
        raise AttachmentError(
            "不支持动画 GIF" if frame_count > 1 else "GIF 数据无效",
            code=code,
        )
    width, height = struct.unpack("<HH", payload[6:10])
    return ImageInfo("image/gif", width, height)


def _gif_frame_count(payload: bytes) -> int:
    packed = payload[10]
    offset = 13
    if packed & 0x80:
        offset += 3 * (2 ** ((packed & 0x07) + 1))
    frames = 0
    while offset < len(payload):
        marker = payload[offset]
        offset += 1
        if marker == 0x3B:
            return frames
        if marker == 0x21:
            if offset >= len(payload):
                return 0
            offset += 1
            offset = _skip_gif_sub_blocks(payload, offset)
            if offset < 0:
                return 0
            continue
        if marker != 0x2C or offset + 9 > len(payload):
            return 0
        frames += 1
        descriptor_packed = payload[offset + 8]
        offset += 9
        if descriptor_packed & 0x80:
            offset += 3 * (2 ** ((descriptor_packed & 0x07) + 1))
        if offset >= len(payload):
            return 0
        offset += 1
        offset = _skip_gif_sub_blocks(payload, offset)
        if offset < 0:
            return 0
    return 0


def _skip_gif_sub_blocks(payload: bytes, offset: int) -> int:
    while offset < len(payload):
        size = payload[offset]
        offset += 1
        if size == 0:
            return offset
        offset += size
        if offset > len(payload):
            return -1
    return -1


def _jpeg_info(payload: bytes) -> ImageInfo:
    offset = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        marker = payload[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(payload):
            break
        length = int.from_bytes(payload[offset : offset + 2], "big")
        if length < 2 or offset + length > len(payload):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            return ImageInfo("image/jpeg", width, height)
        offset += length
    raise AttachmentError("JPEG 尺寸信息无效", code="IMAGE_SIGNATURE_INVALID")


def _webp_info(payload: bytes) -> ImageInfo:
    chunk = payload[12:16]
    data = payload[20:]
    if chunk == b"VP8X" and len(data) >= 10:
        if data[0] & 0x02:
            raise AttachmentError("不支持动画 WebP", code="ANIMATED_WEBP_UNSUPPORTED")
        width = 1 + int.from_bytes(data[4:7], "little")
        height = 1 + int.from_bytes(data[7:10], "little")
    elif chunk == b"VP8L" and len(data) >= 5 and data[0] == 0x2F:
        bits = int.from_bytes(data[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif chunk == b"VP8 " and len(data) >= 10 and data[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(data[6:8], "little") & 0x3FFF
        height = int.from_bytes(data[8:10], "little") & 0x3FFF
    else:
        raise AttachmentError("WebP 文件头无效", code="IMAGE_SIGNATURE_INVALID")
    return ImageInfo("image/webp", width, height)


class AttachmentService:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.root = self.settings.private_object_root.resolve()

    def create(
        self,
        *,
        user_id: str,
        thread_id: str | None,
        payload: bytes,
        claimed_mime: str | None,
        now: datetime | None = None,
    ) -> AIChatAttachment:
        current = now or datetime.now(UTC)
        if thread_id is not None:
            owned = self.session.scalar(
                select(AIChatThread.thread_id).where(
                    AIChatThread.thread_id == thread_id,
                    AIChatThread.user_id == user_id,
                )
            )
            if owned is None:
                raise AttachmentError("对话不存在", code="CHAT_THREAD_NOT_FOUND")
        info = inspect_image(payload, claimed_mime)
        row = AIChatAttachment(
            user_id=user_id,
            thread_id=thread_id,
            mime_type=info.mime_type,
            byte_size=len(payload),
            width=info.width,
            height=info.height,
            content_sha256=sha256_bytes(payload),
            encrypted_object_uri="pending",
            encryption_key_id="pending",
            uploaded_at=current,
            expires_at=current + IMAGE_RETENTION,
        )
        self.session.add(row)
        self.session.flush()
        encrypted, key_id = self._encrypt(user_id, row.attachment_id, "original", payload)
        model_encrypted, _ = self._encrypt(user_id, row.attachment_id, "model", payload)
        row.encrypted_object_uri = self._write(
            user_id, row.attachment_id, "original.enc", encrypted
        )
        row.model_object_uri = self._write(user_id, row.attachment_id, "model.enc", model_encrypted)
        row.encryption_key_id = key_id
        self.session.commit()
        return row

    def get_owned(self, user_id: str, attachment_id: str) -> AIChatAttachment | None:
        return self.session.scalar(
            select(AIChatAttachment).where(
                AIChatAttachment.attachment_id == attachment_id,
                AIChatAttachment.user_id == user_id,
            )
        )

    def read(self, user_id: str, attachment_id: str, now: datetime | None = None) -> bytes:
        row = self.get_owned(user_id, attachment_id)
        current = now or datetime.now(UTC)
        if row is None:
            raise AttachmentError("图片不存在", code="IMAGE_NOT_FOUND")
        if row.deleted_at is not None or row.expires_at <= current:
            raise AttachmentError("图片已按七天保留策略销毁", code="IMAGE_EXPIRED")
        encrypted = self._read_uri(row.encrypted_object_uri)
        return self._decrypt(user_id, attachment_id, "original", encrypted, row.encryption_key_id)

    def model_data_url(self, row: AIChatAttachment, now: datetime | None = None) -> str | None:
        current = now or datetime.now(UTC)
        if row.deleted_at is not None or row.expires_at <= current or not row.model_object_uri:
            return None
        encrypted = self._read_uri(row.model_object_uri)
        payload = self._decrypt(
            row.user_id, row.attachment_id, "model", encrypted, row.encryption_key_id
        )
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{row.mime_type};base64,{encoded}"

    def purge(self, row: AIChatAttachment, *, reason: str, now: datetime | None = None) -> None:
        if row.deleted_at is not None:
            return
        for uri in (row.encrypted_object_uri, row.thumbnail_object_uri, row.model_object_uri):
            if uri:
                path = self._path_from_uri(uri)
                if path.exists():
                    path.unlink()
        row.deleted_at = now or datetime.now(UTC)
        row.deletion_reason = reason
        row.encrypted_object_uri = "deleted"
        row.thumbnail_object_uri = None
        row.model_object_uri = None

    def purge_thread(self, user_id: str, thread_id: str) -> int:
        rows = self.session.scalars(
            select(AIChatAttachment).where(
                AIChatAttachment.user_id == user_id,
                AIChatAttachment.thread_id == thread_id,
                AIChatAttachment.deleted_at.is_(None),
            )
        ).all()
        for row in rows:
            self.purge(row, reason="THREAD_DELETED")
        self.session.commit()
        return len(rows)

    def cleanup_expired(self, now: datetime | None = None) -> int:
        current = now or datetime.now(UTC)
        rows = self.session.scalars(
            select(AIChatAttachment).where(
                AIChatAttachment.expires_at <= current,
                AIChatAttachment.deleted_at.is_(None),
            )
        ).all()
        for row in rows:
            self.purge(row, reason="RETENTION_EXPIRED", now=current)
        self.session.commit()
        return len(rows)

    def _keys(self) -> list[tuple[str, bytes]]:
        configured = (
            self.settings.personal_data_encryption_keys
            or self.settings.model_settings_encryption_keys
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
                raise AttachmentError(
                    "个人数据加密密钥无效", code="DATA_ENCRYPTION_NOT_CONFIGURED"
                ) from exc
            if len(raw) != 32:
                raise AttachmentError(
                    "个人数据加密密钥无效", code="DATA_ENCRYPTION_NOT_CONFIGURED"
                )
            keys.append((sha256_bytes(raw)[:16], raw))
        if not keys:
            raise AttachmentError(
                "个人数据加密尚未配置", code="DATA_ENCRYPTION_NOT_CONFIGURED"
            )
        return keys

    def _encrypt(
        self, user_id: str, attachment_id: str, variant: str, payload: bytes
    ) -> tuple[bytes, str]:
        key_id, key = self._keys()[0]
        nonce = os.urandom(12)
        aad = f"{user_id}:{attachment_id}:{variant}".encode()
        return nonce + AESGCM(key).encrypt(nonce, payload, aad), key_id

    def _decrypt(
        self,
        user_id: str,
        attachment_id: str,
        variant: str,
        encrypted: bytes,
        expected_key_id: str,
    ) -> bytes:
        aad = f"{user_id}:{attachment_id}:{variant}".encode()
        for key_id, key in self._keys():
            if key_id == expected_key_id:
                return AESGCM(key).decrypt(encrypted[:12], encrypted[12:], aad)
        raise AttachmentError("图片加密密钥不可用", code="DATA_ENCRYPTION_KEY_UNAVAILABLE")

    def _write(self, user_id: str, attachment_id: str, name: str, payload: bytes) -> str:
        directory = (self.root / user_id / "attachments" / attachment_id).resolve()
        if not directory.is_relative_to(self.root):
            raise AttachmentError("存储路径无效", code="PRIVATE_STORAGE_ERROR")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(payload)
        return path.as_uri()

    def _path_from_uri(self, uri: str) -> Path:
        if not uri.startswith("file://"):
            raise AttachmentError("私有对象 URI 无效", code="PRIVATE_STORAGE_ERROR")
        parsed = urlparse(uri)
        raw_path = unquote(parsed.path)
        if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        path = Path(raw_path).resolve()
        if not path.is_relative_to(self.root):
            raise AttachmentError("私有对象越界", code="PRIVATE_STORAGE_ERROR")
        return path

    def _read_uri(self, uri: str) -> bytes:
        return self._path_from_uri(uri).read_bytes()
