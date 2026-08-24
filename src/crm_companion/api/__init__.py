"""The HTTP surface. Routes and the OpenAPI document are both generated from the registry."""

from __future__ import annotations

from crm_companion.api.app import create_app

__all__ = ["create_app"]
