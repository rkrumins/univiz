"""Identity provider registry — pluggable per-protocol implementations."""
from .base import IdentityProvider, ProviderCredentials, ProviderIdentity
from .local import LocalIdentityProvider
from .oidc import OidcProvider, load_oidc_settings

_REGISTRY: dict[str, IdentityProvider] = {}


def register_provider(name: str, provider: IdentityProvider) -> None:
    """Register a provider under its ``auth_provider`` value (e.g. 'local', 'oidc')."""
    _REGISTRY[name] = provider


def get_provider(name: str) -> IdentityProvider:
    """Look up a provider by name. Raises ``KeyError`` if not registered."""
    return _REGISTRY[name]


def known_providers() -> list[str]:
    return sorted(_REGISTRY.keys())


__all__ = [
    "IdentityProvider",
    "ProviderCredentials",
    "ProviderIdentity",
    "LocalIdentityProvider",
    "OidcProvider",
    "load_oidc_settings",
    "register_provider",
    "get_provider",
    "known_providers",
]
