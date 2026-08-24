"""OpenAPI export.

The document is a committed artifact because it is what gets registered with the
Foundry agent. Regenerating it is how a tool change reaches the agent, so a stale
file is a real defect rather than a cosmetic one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crm_companion.api.app import create_app
from crm_companion.config import Settings

__all__ = ["DEFAULT_SPEC_PATH", "build_spec", "write_spec"]

DEFAULT_SPEC_PATH = Path("openapi/crm-tools.json")


def build_spec(settings: Settings | None = None) -> dict[str, Any]:
    return create_app(settings).openapi()


def write_spec(path: Path = DEFAULT_SPEC_PATH, settings: Settings | None = None) -> dict[str, Any]:
    spec = build_spec(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return spec
