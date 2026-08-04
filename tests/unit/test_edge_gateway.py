from cryptography.fernet import Fernet
import pytest

from ashare_ai.core.config import Settings
from ashare_ai.core.edge_gateway import EdgeGatewayError, render_nginx, validate_frpc_toml, validate_proxy_hosts


def _settings() -> Settings:
    return Settings(edge_gateway_encryption_keys=Fernet.generate_key().decode(), edge_domain="research.example.com")


def test_frpc_requires_loopback_gateway_targets() -> None:
    parsed = validate_frpc_toml(
        'serverAddr = "frps.example.com"\nserverPort = 7000\n[[proxies]]\nname = "web"\nlocalIP = "127.0.0.1"\nlocalPort = 80\ncustomDomains = ["research.example.com"]',
        _settings(),
    )
    assert parsed["serverPort"] == 7000


def test_frpc_rejects_non_gateway_port() -> None:
    with pytest.raises(EdgeGatewayError):
        validate_frpc_toml('serverAddr = "frps"\nserverPort = 7000\n[[proxies]]\nname = "bad"\nlocalIP = "127.0.0.1"\nlocalPort = 8080', _settings())


def test_proxy_rendering_has_no_raw_directive_surface() -> None:
    hosts = validate_proxy_hosts([{"name": "web", "domains": ["research.example.com"], "forward_host": "web", "forward_port": 80}], _settings())
    rendered = render_nginx(hosts, _settings())
    assert "proxy_pass http://web:80;" in rendered
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in rendered
