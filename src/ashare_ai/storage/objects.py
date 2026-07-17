from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast
from urllib.parse import unquote, urlparse

from ashare_ai.core.hashing import sha256_bytes


class ObjectStore(Protocol):
    def put(self, payload: bytes, *, content_type: str) -> tuple[str, str]: ...

    def get(self, uri: str) -> bytes: ...


class LocalObjectStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> tuple[str, str]:
        del content_type
        digest = sha256_bytes(payload)
        path = self.root / "sha256" / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(payload)
        return path.resolve().as_uri(), digest

    def get(self, uri: str) -> bytes:
        if uri.startswith("file://"):
            parsed = urlparse(uri)
            raw_path = unquote(parsed.path)
            if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
                raw_path = raw_path[1:]
            path = Path(raw_path)
        else:
            path = Path(uri)
        payload = path.read_bytes()
        expected = path.name
        actual = sha256_bytes(payload)
        if expected != actual:
            raise ValueError(f"object hash mismatch: expected={expected}, actual={actual}")
        return payload


class S3ObjectStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        secure: bool = True,
    ) -> None:
        import boto3

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            use_ssl=secure,
        )

    def put(
        self, payload: bytes, *, content_type: str = "application/octet-stream"
    ) -> tuple[str, str]:
        digest = sha256_bytes(payload)
        key = f"objects/sha256/{digest[:2]}/{digest}"
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        return f"s3://{self.bucket}/{key}", digest

    def get(self, uri: str) -> bytes:
        prefix = f"s3://{self.bucket}/"
        if not uri.startswith(prefix):
            raise ValueError(f"URI is outside configured bucket: {uri}")
        key = uri.removeprefix(prefix)
        payload = cast(bytes, self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())
        if sha256_bytes(payload) != key.rsplit("/", 1)[-1]:
            raise ValueError("object hash mismatch")
        return payload
