"""Write the OpenAPI document the Foundry agent is registered against."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crm_companion.api.openapi import DEFAULT_SPEC_PATH, write_spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_SPEC_PATH)
    args = parser.parse_args()

    spec = write_spec(args.output)
    print(f"wrote {args.output}: {len(spec['paths'])} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
