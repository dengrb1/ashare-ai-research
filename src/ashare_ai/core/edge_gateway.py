from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.storage.models import (
    ActiveEdgeGatewayConfiguration,
    EdgeGatewayConfigurationVersion,
)

MAX_TOML_BYTES = 64 * 1024
MAX_PROXY_HOSTS = 32
MAX_LOG_LINES = 300
MAX_LOG_BYTES = 128 * 1024
_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SENSITIVE_LOG_VALUE = re.compile(
    r"(?i)(authorization|auth(?:\.token)?|token|password|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)


class EdgeGatewayError(ValueError):
    pass


def _sanitize_log_line(value: str) -> str:
    sanitized = _SENSITIVE_LOG_VALUE.sub(r"\1=[REDACTED]", value.strip())
    return sanitized[:2000]


def _cipher(settings: Settings) -> tuple[Fernet, str]:
    raw = settings.edge_gateway_encryption_keys
    if not raw:
        raise EdgeGatewayError("EDGE_GATEWAY_ENCRYPTION_KEYS is required")
    key = raw.split(",", 1)[0].strip().encode()
    try:
        return Fernet(key), hashlib.sha256(key).hexdigest()[:16]
    except Exception as exc:
        raise EdgeGatewayError("EDGE_GATEWAY_ENCRYPTION_KEYS is invalid") from exc


def validate_frpc_toml(
    value: str, settings: Settings | None = None, *, strict: bool = True
) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise EdgeGatewayError("FRP TOML cannot be empty")
    if len(value.encode()) > MAX_TOML_BYTES:
        raise EdgeGatewayError("FRP TOML exceeds 64 KiB")
    try:
        parsed = tomllib.loads(value)
    except tomllib.TOMLDecodeError as exc:
        raise EdgeGatewayError(f"invalid FRP TOML: {exc}") from exc
    common_value = parsed.get("common")
    common: dict[str, Any] = common_value if not strict and isinstance(common_value, dict) else {}
    server_addr = parsed.get("serverAddr")
    server_port = parsed.get("serverPort")
    if not strict:
        # FRP 0.x commonly stores connection settings below [common], while
        # some transitional configs put the snake_case keys at the root.
        server_addr = server_addr or common.get("server_addr") or parsed.get("server_addr")
        server_port = (
            server_port
            if server_port is not None
            else common.get("server_port", parsed.get("server_port"))
        )
    if not server_addr or not isinstance(server_port, int):
        raise EdgeGatewayError(
            "serverAddr/serverPort are required (strict mode uses camelCase; "
            "compatibility mode also accepts snake_case)"
        )
    proxies = parsed.get("proxies", [])
    if not strict and not proxies:
        proxies = [
            {"name": key, **item}
            for key, item in parsed.items()
            if key
            not in {"common", "auth", "serverAddr", "serverPort", "server_addr", "server_port"}
            and isinstance(item, dict)
        ]
    if not isinstance(proxies, list) or len(proxies) > MAX_PROXY_HOSTS:
        raise EdgeGatewayError("FRP proxies must be a list of at most 32 entries")
    for proxy in proxies:
        if not isinstance(proxy, dict) or not proxy.get("name"):
            raise EdgeGatewayError("each FRP proxy needs a name")
        local_ip = proxy.get("localIP", "127.0.0.1")
        local_port = proxy.get("localPort")
        domains = proxy.get("customDomains", [])
        if not strict:
            local_ip = proxy.get("localIP", proxy.get("local_ip", "127.0.0.1"))
            local_port = proxy.get("localPort", proxy.get("local_port"))
            domains = proxy.get("customDomains", proxy.get("custom_domains", []))
            if isinstance(domains, str):
                domains = [domains]
        if local_ip != "127.0.0.1":
            raise EdgeGatewayError("FRP localIP must be 127.0.0.1")
        if local_port not in {80, 443}:
            raise EdgeGatewayError("FRP localPort must be 80 or 443")
    settings = settings or get_settings()
    if settings.edge_domain:
        for proxy in proxies:
            domains = proxy.get("customDomains", [])
            if not strict:
                domains = proxy.get("customDomains", proxy.get("custom_domains", []))
                if isinstance(domains, str):
                    domains = [domains]
            if any(domain != settings.edge_domain for domain in domains):
                raise EdgeGatewayError("FRP customDomains must match EDGE_DOMAIN")
    return parsed


def validate_proxy_hosts(
    hosts: list[dict[str, Any]], settings: Settings | None = None
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    if len(hosts) > MAX_PROXY_HOSTS:
        raise EdgeGatewayError("at most 32 proxy hosts are allowed")
    allowed = {
        item.strip() for item in settings.edge_proxy_target_allowlist.split(",") if item.strip()
    }
    normalized: list[dict[str, Any]] = []
    for host in hosts:
        domains = host.get("domains") or []
        target = str(host.get("forward_host", "")).strip()
        if not domains or any(
            not isinstance(domain, str) or not _DOMAIN.fullmatch(domain) for domain in domains
        ):
            raise EdgeGatewayError("proxy domains are invalid")
        if target not in allowed:
            try:
                address = ipaddress.ip_address(target)
            except ValueError as exc:
                raise EdgeGatewayError(f"proxy target {target!r} is not allowlisted") from exc
            if not (address.is_private or address.is_loopback):
                raise EdgeGatewayError("proxy IP must be private or loopback")
        port = int(host.get("forward_port", 80))
        if port < 1 or port > 65535:
            raise EdgeGatewayError("proxy port must be between 1 and 65535")
        normalized.append(
            {
                "id": str(
                    host.get("id")
                    or stable_hash({"domains": domains, "target": target, "port": port})[:12]
                ),
                "name": str(host.get("name") or domains[0])[:80],
                "domains": domains[:8],
                "forward_scheme": "https" if host.get("forward_scheme") == "https" else "http",
                "forward_host": target,
                "forward_port": port,
                "ssl_enabled": bool(host.get("ssl_enabled", True)),
                "websocket_support": bool(host.get("websocket_support", True)),
                "enabled": bool(host.get("enabled", True)),
                "notes": str(host.get("notes") or "")[:240],
            }
        )
    return normalized


def render_nginx(hosts: list[dict[str, Any]], settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    lines = ["limit_conn_zone $binary_remote_addr zone=edge_per_ip:1m;", ""]
    for host in hosts:
        if not host["enabled"]:
            continue
        names = " ".join(host["domains"])
        protocol = host["forward_scheme"]
        upstream = f"{protocol}://{host['forward_host']}:{host['forward_port']}"
        lines.extend(
            [
                "server {",
                "    listen 80;",
                f"    server_name {names};",
                "    location ^~ /.well-known/acme-challenge/ { "
                "root /var/lib/acme-webroot; try_files $uri =404; }",
                "    location / { return 308 https://$host$request_uri; }",
                "}",
                "",
            ]
        )
        if host["ssl_enabled"]:
            lines.extend(
                [
                    "server {",
                    "    listen 443 ssl;",
                    f"    server_name {names};",
                    "    ssl_certificate /etc/edge/certs/fullchain.pem;",
                    "    ssl_certificate_key /etc/edge/certs/key.pem;",
                    "    ssl_protocols TLSv1.2 TLSv1.3;",
                    "    add_header X-Content-Type-Options nosniff always;",
                    "    add_header X-Frame-Options DENY always;",
                    "    limit_conn edge_per_ip 32;",
                    "    location / {",
                    f"        proxy_pass {upstream};",
                    "        proxy_http_version 1.1;",
                    "        proxy_set_header Host $host;",
                    "        proxy_set_header X-Real-IP $remote_addr;",
                    "        proxy_set_header X-Forwarded-For $remote_addr;",
                    "        proxy_set_header X-Forwarded-Proto https;",
                    *(
                        [
                            "        proxy_set_header Upgrade $http_upgrade;",
                            '        proxy_set_header Connection "upgrade";',
                        ]
                        if host["websocket_support"]
                        else []
                    ),
                    "    }",
                    "}",
                    "",
                ]
            )
    if not any(host["enabled"] for host in hosts):
        domain = settings.edge_domain or "_"
        lines.extend(
            [
                "server {",
                "    listen 80 default_server;",
                f"    server_name {domain};",
                "    return 444;",
                "}",
                "",
            ]
        )
    return "\n".join(lines)


class EdgeGatewayConfigurationService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._syncing = False

    def _row(self, db: Session) -> EdgeGatewayConfigurationVersion | None:
        pointer = db.get(ActiveEdgeGatewayConfiguration, "default")
        return (
            db.get(EdgeGatewayConfigurationVersion, pointer.configuration_id) if pointer else None
        )

    def public_view(self, db: Session, *, reveal: bool = False) -> dict[str, Any]:
        self.sync_from_files(db)
        row = self._row(db)
        if row is None:
            return {
                "version": 0,
                "enabled": False,
                "proxy_hosts": [],
                "frpc_toml": "",
                "config_sha256": None,
                "apply_status": "UNCONFIGURED",
                "source_sync": False,
            }
        cipher, _ = _cipher(self.settings)
        try:
            toml = cipher.decrypt(row.encrypted_frpc_toml.encode()).decode() if reveal else ""
        except InvalidToken as exc:
            raise EdgeGatewayError("edge gateway configuration cannot be decrypted") from exc
        return {
            "configuration_id": row.configuration_id,
            "version": row.version,
            "enabled": row.enabled,
            "validation_mode": row.validation_mode,
            "proxy_hosts": row.proxy_hosts,
            "frpc_toml": toml,
            "config_sha256": row.config_sha256,
            "apply_status": row.apply_status,
            "apply_message": row.apply_message,
            "applied_at": row.applied_at,
            "applied_sha256": row.applied_sha256,
            "source_sync": True,
        }

    def _source_dir(self) -> Path:
        path = self.settings.edge_gateway_source_dir
        return path if path.is_absolute() else Path.cwd() / path

    def _log_dir(self) -> Path:
        path = self.settings.edge_gateway_log_dir
        return path if path.is_absolute() else Path.cwd() / path

    def read_frp_logs(self, *, limit: int = MAX_LOG_LINES) -> dict[str, Any]:
        log_path = self._log_dir() / "frpc.log"
        if not log_path.is_file():
            return {
                "available": False,
                "message": "FRP 服务尚未启动，暂时没有运行日志。",
                "lines": [],
                "updated_at": None,
            }
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - MAX_LOG_BYTES))
                content = handle.read().decode("utf-8", errors="replace")
            updated_at = datetime.fromtimestamp(log_path.stat().st_mtime, UTC)
        except OSError:
            return {
                "available": False,
                "message": "FRP 日志暂时无法读取。",
                "lines": [],
                "updated_at": None,
            }
        lines = [_sanitize_log_line(line) for line in content.splitlines()]
        return {
            "available": True,
            "message": "已读取最近的 FRP 运行日志。",
            "lines": lines[-max(1, min(limit, MAX_LOG_LINES)) :],
            "updated_at": updated_at,
        }

    def _read_external(self) -> tuple[str | None, list[dict[str, Any]] | None]:
        directory = self._source_dir()
        frpc_path = directory / "frpc.toml"
        managed_path = directory / "managed.conf"
        frpc = frpc_path.read_text(encoding="utf-8") if frpc_path.is_file() else None
        hosts: list[dict[str, Any]] | None = None
        if managed_path.is_file():
            text = managed_path.read_text(encoding="utf-8")
            blocks = re.findall(r"server\\s*\\{(.*?)\\}", text, re.DOTALL)
            parsed: list[dict[str, Any]] = []
            for block in blocks:
                names = re.search(r"server_name\\s+([^;]+);", block)
                proxy = re.search(r"proxy_pass\\s+(https?)://([^:;]+):(\\d+);", block)
                if not names or not proxy:
                    continue
                domains = names.group(1).split()
                parsed.append(
                    {
                        "name": domains[0],
                        "domains": domains,
                        "forward_scheme": proxy.group(1),
                        "forward_host": proxy.group(2),
                        "forward_port": int(proxy.group(3)),
                        "ssl_enabled": "listen 443 ssl" in block,
                        "websocket_support": "Upgrade" in block,
                        "enabled": True,
                        "notes": "",
                    }
                )
            if parsed:
                hosts = validate_proxy_hosts(parsed, self.settings)
        return frpc, hosts

    def sync_from_files(self, db: Session) -> None:
        if self._syncing:
            return
        frpc, external_hosts = self._read_external()
        if frpc is None and external_hosts is None:
            return
        row = self._row(db)
        if row is None:
            if frpc is None:
                return
            hosts = external_hosts or []
            try:
                self._syncing = True
                try:
                    self.save(
                        db,
                        enabled=False,
                        proxy_hosts=hosts,
                        frpc_toml=frpc,
                        user_id=None,
                        write_files=False,
                    )
                except EdgeGatewayError:
                    # Existing operator-managed files may use older FRP field
                    # names. Keep the same safety checks while preserving them
                    # in compatibility mode so the UI can make the mode explicit.
                    self.save(
                        db,
                        enabled=False,
                        validation_mode="COMPATIBLE",
                        proxy_hosts=hosts,
                        frpc_toml=frpc,
                        user_id=None,
                        write_files=False,
                    )
                db.commit()
            finally:
                self._syncing = False
            return
        cipher, _ = _cipher(self.settings)
        current_frpc = cipher.decrypt(row.encrypted_frpc_toml.encode()).decode()
        next_frpc = frpc if frpc is not None else current_frpc
        next_hosts = external_hosts if external_hosts is not None else row.proxy_hosts
        if next_frpc == current_frpc and next_hosts == row.proxy_hosts:
            return
        try:
            self._syncing = True
            self.save(
                db,
                enabled=row.enabled,
                proxy_hosts=next_hosts,
                frpc_toml=next_frpc,
                user_id=None,
                write_files=False,
                validation_mode=row.validation_mode,
            )
            db.commit()
        finally:
            self._syncing = False

    def save(
        self,
        db: Session,
        *,
        enabled: bool,
        proxy_hosts: list[dict[str, Any]],
        frpc_toml: str,
        user_id: str | None,
        write_files: bool = True,
        validation_mode: str = "STRICT",
    ) -> dict[str, Any]:
        hosts = validate_proxy_hosts(proxy_hosts, self.settings)
        if validation_mode not in {"STRICT", "COMPATIBLE"}:
            raise EdgeGatewayError("FRP validation mode is invalid")
        validate_frpc_toml(frpc_toml, self.settings, strict=validation_mode == "STRICT")
        cipher, key_id = _cipher(self.settings)
        version = int(db.scalar(select(func.max(EdgeGatewayConfigurationVersion.version))) or 0) + 1
        payload = {
            "enabled": enabled,
            "validation_mode": validation_mode,
            "proxy_hosts": hosts,
            "frpc_toml": frpc_toml,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        row = EdgeGatewayConfigurationVersion(
            version=version,
            enabled=enabled,
            validation_mode=validation_mode,
            proxy_hosts=hosts,
            encrypted_frpc_toml=cipher.encrypt(frpc_toml.encode()).decode(),
            encryption_key_id=key_id,
            config_sha256=digest,
            created_by=user_id,
            created_at=datetime.now(UTC),
            apply_status="PENDING",
        )
        db.add(row)
        pointer = db.get(ActiveEdgeGatewayConfiguration, "default")
        if pointer is None:
            pointer = ActiveEdgeGatewayConfiguration(
                scope="default",
                configuration_id=row.configuration_id,
                activated_by=user_id,
                activated_at=datetime.now(UTC),
            )
            db.add(pointer)
        else:
            pointer.configuration_id = row.configuration_id
            pointer.activated_by = user_id
            pointer.activated_at = datetime.now(UTC)
        db.flush()
        if write_files:
            self._write_files(hosts, frpc_toml)
        return self.public_view(db, reveal=True)

    def _write_files(self, hosts: list[dict[str, Any]], frpc_toml: str) -> None:
        directory = self._source_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            for name, content in (
                ("frpc.toml", frpc_toml),
                ("managed.conf", render_nginx(hosts, self.settings)),
            ):
                target = directory / name
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(content, encoding="utf-8")
                temporary.replace(target)
        except OSError:
            # The host controller remains the authoritative writer when the
            # container mount is read-only; the database version is retained.
            return

    def internal_payload(self, db: Session) -> dict[str, Any]:
        view = self.public_view(db, reveal=True)
        view["nginx_conf"] = render_nginx(view["proxy_hosts"], self.settings)
        return view

    def mark_applied(
        self, db: Session, config_id: str, sha256: str, status: str, message: str | None
    ) -> None:
        row = db.get(EdgeGatewayConfigurationVersion, config_id)
        if row:
            row.applied_at = datetime.now(UTC)
            row.applied_sha256 = sha256
            row.apply_status = status
            row.apply_message = message
