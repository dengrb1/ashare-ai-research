import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ashare_ai.core.config import Settings
from ashare_ai.core.edge_gateway import (
    EdgeGatewayConfigurationService,
    EdgeGatewayError,
    render_nginx,
    validate_frpc_toml,
    validate_proxy_hosts,
)
from ashare_ai.storage.models import Base


def _settings(source_dir=None, log_dir=None) -> Settings:
    return Settings(
        edge_gateway_encryption_keys=Fernet.generate_key().decode(),
        edge_domain="research.example.com",
        edge_gateway_source_dir=source_dir or "docker/edge-gateway",
        edge_gateway_log_dir=log_dir or ".secrets/edge-gateway-logs",
    )


def test_frpc_requires_loopback_gateway_targets() -> None:
    parsed = validate_frpc_toml(
        'serverAddr = "frps.example.com"\nserverPort = 7000\n[[proxies]]\n'
        'name = "web"\nlocalIP = "127.0.0.1"\nlocalPort = 80\n'
        'customDomains = ["research.example.com"]',
        _settings(),
    )
    assert parsed["serverPort"] == 7000


def test_frpc_rejects_non_gateway_port() -> None:
    with pytest.raises(EdgeGatewayError):
        validate_frpc_toml(
            'serverAddr = "frps"\nserverPort = 7000\n[[proxies]]\n'
            'name = "bad"\nlocalIP = "127.0.0.1"\nlocalPort = 8080',
            _settings(),
        )


def test_compatible_frpc_accepts_legacy_common_and_proxy_sections() -> None:
    parsed = validate_frpc_toml(
        '[common]\nserver_addr = "frps.example.com"\nserver_port = 7000\n\n'
        '[ashare_edge_http_kr]\ntype = "http"\nlocal_ip = "127.0.0.1"\n'
        'local_port = 80\ncustom_domains = "research.example.com"\n',
        _settings(),
        strict=False,
    )
    assert parsed["common"]["server_port"] == 7000


def test_compatible_frpc_still_rejects_non_loopback_targets() -> None:
    with pytest.raises(EdgeGatewayError, match=r"127\.0\.0\.1"):
        validate_frpc_toml(
            '[common]\nserver_addr = "frps.example.com"\nserver_port = 7000\n\n'
            '[legacy]\ntype = "http"\nlocal_ip = "10.0.0.8"\nlocal_port = 80\n',
            _settings(),
            strict=False,
        )


def test_proxy_rendering_has_no_raw_directive_surface() -> None:
    hosts = validate_proxy_hosts(
        [{
            "name": "web",
            "domains": ["research.example.com"],
            "forward_host": "web",
            "forward_port": 80,
        }],
        _settings(),
    )
    rendered = render_nginx(hosts, _settings())
    assert "proxy_pass http://web:80;" in rendered
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in rendered


def test_external_frpc_file_is_imported_as_a_new_version(tmp_path) -> None:
    source = tmp_path / "edge-gateway"
    source.mkdir()
    (source / "frpc.toml").write_text('serverAddr = "frps"\nserverPort = 7000\n', encoding="utf-8")
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    settings = _settings(source)
    with Session(engine) as session:
        view = EdgeGatewayConfigurationService(settings).public_view(session, reveal=True)
        assert view["version"] == 1
        assert "serverAddr" in view["frpc_toml"]


def test_frp_logs_are_available_and_redacted(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "frpc.log").write_text(
        "2026-08-04 login token=super-secret status=ok\n",
        encoding="utf-8",
    )
    result = EdgeGatewayConfigurationService(_settings(log_dir=log_dir)).read_frp_logs()
    assert result["available"] is True
    assert "super-secret" not in result["lines"][0]
    assert "REDACTED" in result["lines"][0]


def test_frp_logs_report_missing_file(tmp_path) -> None:
    result = EdgeGatewayConfigurationService(
        _settings(log_dir=tmp_path / "missing")
    ).read_frp_logs()
    assert result["available"] is False
    assert result["lines"] == []
