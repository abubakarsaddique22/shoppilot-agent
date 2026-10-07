"""Which store backend the app uses (Step E): SHOP_STORE_BACKEND is "mock" (default) or "shopify".

Every place that needs a ShopBackend (API lifespan, jobs, scripts) calls make_shop(), so switching the store is one
setting and never a code edit. Evaluation keeps using MockShop directly, because its data is the same every run.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from shoppilot.core.config import settings
from shoppilot.core.errors import ConfigError
from shoppilot.shop.base import ShopBackend
from shoppilot.shop.mockshop import MockShop, utc_now


def make_shop(session_factory: sessionmaker[Session], now: Callable[[], datetime] = utc_now) -> ShopBackend:
    backend = settings.store_backend.strip().lower()
    if backend == "mock":
        return MockShop(session_factory, now=now)
    if backend == "shopify":
        from shoppilot.shop.shopify import ShopifyBackend  # imported here: the mock path never needs httpx

        return ShopifyBackend(
            domain=settings.shopify_store_domain,
            client_id=settings.shopify_client_id,
            client_secret=settings.shopify_client_secret,
            api_version=settings.shopify_api_version,
            session_factory=session_factory,
            now=now,
        )
    raise ConfigError(f"SHOP_STORE_BACKEND must be 'mock' or 'shopify', not '{backend}'")
