from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import tomllib
from datetime import UTC, datetime
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
_DOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class EdgeGatewayError(ValueError):
    pass


def _cipher(settings: Settings) -> tuple[Fernet, str]:
    raw = settings.edge_gateway_encryption_keys
    if not raw:
        raise EdgeGatewayError("EDGE_GATEWAY_ENCRYPTION_KEYS is required")
    key = raw.split(",", 1)[0].strip().encode()
    try:
        return Fernet(key), hashlib.sha256(key).hexdigest()[:16]
    except Exception as exc:
        raise EdgeGatewayError("EDGE_GATEWAY_ENCRYPTION_KEYS is invalid") from exc


def validate_frpc_toml(value: str, settings: Settings | None = None) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        raise EdgeGatewayError("FRP TOML cannot be empty")
    if len(value.encode()) > MAX_TOML_BYTES:
        raise EdgeGatewayError("FRP TOML exceeds 64 KiB")
    try:
        parsed = tomllib.loads(value)
    except tomllib.TOMLDecodeError as exc:
        raise EdgeGatewayError(f"invalid FRP TOML: {exc}") from exc
    if not parsed.get("serverAddr") or not isinstance(parsed.get("serverPort"), int):
        raise EdgeGatewayError("serverAddr and integer serverPort are required")
    proxies = parsed.get("proxies", [])
    if not isinstance(proxies, list) or len(proxies) > MAX_PROXY_HOSTS:
        raise EdgeGatewayError("FRP proxies must be a list of at most 32 entries")
    for proxy in proxies:
        if not isinstance(proxy, dict) or not proxy.get("name"):
            raise EdgeGatewayError("each FRP proxy needs a name")
        if proxy.get("localIP", "127.0.0.1") != "127.0.0.1":
            raise EdgeGatewayError("FRP localIP must be 127.0.0.1")
        if proxy.get("localPort") not in {80, 443}:
            raise EdgeGatewayError("FRP localPort must be 80 or 443")
    settings = settings or get_settings()
    if settings.edge_domain:
        for proxy in proxies:
            domains = proxy.get("customDomains", [])
            if any(domain != settings.edge_domain for domain in domains):
                raise EdgeGatewayError("FRP customDomains must match EDGE_DOMAIN")
    return parsed


def validate_proxy_hosts(hosts: list[dict[str, Any]], settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    if len(hosts) > MAX_PROXY_HOSTS:
        raise EdgeGatewayError("at most 32 proxy hosts are allowed")
    allowed = {item.strip() for item in settings.edge_proxy_target_allowlist.split(",") if item.strip()}
    normalized: list[dict[str, Any]] = []
    for host in hosts:
        domains = host.get("domains") or []
        target = str(host.get("forward_host", "")).strip()
        if not domains or any(not isinstance(domain, str) or not _DOMAIN.fullmatch(domain) for domain in domains):
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
        normalized.append({
            "id": str(host.get("id") or stable_hash({"domains": domains, "target": target, "port": port})[:12]),
            "name": str(host.get("name") or domains[0])[:80],
            "domains": domains[:8],
            "forward_scheme": "https" if host.get("forward_scheme") == "https" else "http",
            "forward_host": target,
            "forward_port": port,
            "ssl_enabled": bool(host.get("ssl_enabled", True)),
            "websocket_support": bool(host.get("websocket_support", True)),
            "enabled": bool(host.get("enabled", True)),
            "notes": str(host.get("notes") or "")[:240],
        })
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
        lines.extend([
            "server {",
            "    listen 80;",
            f"    server_name {names};",
            "    location ^~ /.well-known/acme-challenge/ { root /var/lib/acme-webroot; try_files $uri =404; }",
            "    location / { return 308 https://$host$request_uri; }",
            "}",
            "",
        ])
        if host["ssl_enabled"]:
            lines.extend([
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
                *( ["        proxy_set_header Upgrade $http_upgrade;", "        proxy_set_header Connection \"upgrade\";"] if host["websocket_support"] else []),
                "    }",
                "}",
                "",
            ])
    if not any(host["enabled"] for host in hosts):
        domain = settings.edge_domain or "_"
        lines.extend(["server {", "    listen 80 default_server;", f"    server_name {domain};", "    return 444;", "}", ""])
    return "\n".join(lines)


class EdgeGatewayConfigurationService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _row(self, db: Session) -> EdgeGatewayConfigurationVersion | None:
        pointer = db.get(ActiveEdgeGatewayConfiguration, "default")
        return db.get(EdgeGatewayConfigurationVersion, pointer.configuration_id) if pointer else None

    def public_view(self, db: Session, *, reveal: bool = False) -> dict[str, Any]:
        row = self._row(db)
        if row is None:
            return {"version": 0, "enabled": False, "proxy_hosts": [], "frpc_toml": "", "config_sha256": None, "apply_status": "UNCONFIGURED"}
        cipher, _ = _cipher(self.settings)
        try:
            toml = cipher.decrypt(row.encrypted_frpc_toml.encode()).decode() if reveal else ""
        except InvalidToken as exc:
            raise EdgeGatewayError("edge gateway configuration cannot be decrypted") from exc
        return {"configuration_id": row.configuration_id, "version": row.version, "enabled": row.enabled, "proxy_hosts": row.proxy_hosts, "frpc_toml": toml, "config_sha256": row.config_sha256, "apply_status": row.apply_status, "apply_message": row.apply_message, "applied_at": row.applied_at, "applied_sha256": row.applied_sha256}

    def save(self, db: Session, *, enabled: bool, proxy_hosts: list[dict[str, Any]], frpc_toml: str, user_id: str | None) -> dict[str, Any]:
        hosts = validate_proxy_hosts(proxy_hosts, self.settings)
        validate_frpc_toml(frpc_toml, self.settings)
        cipher, key_id = _cipher(self.settings)
        previous = self._row(db)
        version = int(db.scalar(select(func.max(EdgeGatewayConfigurationVersion.version))) or 0) + 1
        payload = {"enabled": enabled, "proxy_hosts": hosts, "frpc_toml": frpc_toml}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row = EdgeGatewayConfigurationVersion(version=version, enabled=enabled, proxy_hosts=hosts, encrypted_frpc_toml=cipher.encrypt(frpc_toml.encode()).decode(), encryption_key_id=key_id, config_sha256=digest, created_by=user_id, created_at=datetime.now(UTC), apply_status="PENDING")
        db.add(row)
        pointer = db.get(ActiveEdgeGatewayConfiguration, "default")
        if pointer is None:
            pointer = ActiveEdgeGatewayConfiguration(scope="default", configuration_id=row.configuration_id, activated_by=user_id, activated_at=datetime.now(UTC))
            db.add(pointer)
        else:
            pointer.configuration_id = row.configuration_id
            pointer.activated_by = user_id
            pointer.activated_at = datetime.now(UTC)
        db.flush()
        return self.public_view(db, reveal=True)

    def internal_payload(self, db: Session) -> dict[str, Any]:
        view = self.public_view(db, reveal=True)
        view["nginx_conf"] = render_nginx(view["proxy_hosts"], self.settings)
        return view

    def mark_applied(self, db: Session, config_id: str, sha256: str, status: str, message: str | None) -> None:
        row = db.get(EdgeGatewayConfigurationVersion, config_id)
        if row:
            row.applied_at = datetime.now(UTC)
            row.applied_sha256 = sha256
            row.apply_status = status
            row.apply_message = message
