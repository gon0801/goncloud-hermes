"""Hermes provider backed by the owner's exact Goncloud subscription chain."""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


class GoncloudSubscriptionProfile(ProviderProfile):
    """Local in-process client; the external identity carries no API key."""

    def create_client(self, **client_kwargs: Any) -> Any:
        from .client import GoncloudSubscriptionClient

        return GoncloudSubscriptionClient(**client_kwargs)

    def fetch_models(self, **_: Any) -> list[str]:
        return ["main"]


goncloud_subscriptions = GoncloudSubscriptionProfile(
    name="goncloud-subscriptions",
    display_name="Goncloud subscriptions",
    description="Exact local subscription chain from model-routing.source.json",
    api_mode="chat_completions",
    env_vars=(),
    base_url="acp://goncloud-subscriptions",
    auth_type="external_process",
    supports_health_check=False,
    supports_model_listing=False,
    process_command="hermes",
    process_args=(),
    process_command_env_vars=(),
    process_args_env_var="",
    fallback_models=("main",),
)

register_provider(goncloud_subscriptions)

__all__ = ["GoncloudSubscriptionProfile", "goncloud_subscriptions"]
