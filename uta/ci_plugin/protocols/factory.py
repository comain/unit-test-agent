"""Concrete CI protocol registry wiring for the bundled application."""
from __future__ import annotations

from pathlib import Path

from uta.ci_plugin.protocols import ProtocolRegistry
from uta.ci_plugin.protocols.github import (
    GithubAppAuth,
    GithubChecksClient,
    GithubWebhookProtocol,
)


def build_registry(config) -> ProtocolRegistry:
    """Construct the default registry from ``Settings``.

    The open-source application wires the GitHub webhook adapter here. The
    service and protocol abstractions remain unaware of that concrete adapter.
    """
    private_key_pem = ""
    key_path = getattr(config, "github_app_private_key_path", "") or ""
    if key_path:
        path = Path(key_path).expanduser()
        if path.exists():
            private_key_pem = path.read_text(encoding="utf-8")

    app_id = getattr(config, "github_app_id", "") or ""
    api_base_url = getattr(config, "github_api_base_url", "https://api.github.com")
    checks_client = None
    if app_id and private_key_pem:
        checks_client = GithubChecksClient(
            auth=GithubAppAuth(
                app_id=app_id,
                private_key_pem=private_key_pem,
                api_base_url=api_base_url,
            ),
            api_base_url=api_base_url,
            timeout_seconds=getattr(config, "github_callback_timeout_seconds", 10),
            retry_times=getattr(config, "github_callback_retry_times", 3),
        )

    return ProtocolRegistry(
        [
            GithubWebhookProtocol(
                webhook_secret=getattr(config, "github_webhook_secret", "") or "",
                checks_client=checks_client,
                check_name=getattr(config, "github_check_name", "uta/unit-test-enforcement"),
            )
        ]
    )
