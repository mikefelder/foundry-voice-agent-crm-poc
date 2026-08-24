"""Create the Foundry project connection holding the tool API key.

The agent presents this key when calling the OpenAPI tool. It lives in a project
connection rather than in the agent definition so the key is never part of the
tool schema, and so rotating it does not require a new agent version.

``azure-ai-projects`` can read connections but not create them, so this goes
straight to ARM.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx
from azure.identity import DefaultAzureCredential

ARM = "https://management.azure.com"
API_VERSION = "2025-04-01-preview"
DEFAULT_NAME = "crm-tools-api"


def create_connection(
    *,
    subscription_id: str,
    resource_group: str,
    account: str,
    project: str,
    target: str,
    api_key: str,
    name: str = DEFAULT_NAME,
) -> str:
    credential = DefaultAzureCredential()
    token = credential.get_token(f"{ARM}/.default").token

    url = (
        f"{ARM}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
        f"/projects/{project}/connections/{name}?api-version={API_VERSION}"
    )
    body = {
        "properties": {
            "category": "CustomKeys",
            "authType": "CustomKeys",
            "target": target,
            "isSharedToAll": False,
            "credentials": {"keys": {"x-api-key": api_key}},
            "metadata": {},
        }
    }

    response = httpx.put(
        url,
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"connection create failed [{response.status_code}]: {response.text}")
    return response.json()["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args()

    api_key = os.environ.get("TOOL_API_KEY")
    if not api_key:
        print("TOOL_API_KEY is not set in the environment", file=sys.stderr)
        return 1

    connection_id = create_connection(
        subscription_id=args.subscription_id,
        resource_group=args.resource_group,
        account=args.account,
        project=args.project,
        target=args.target,
        api_key=api_key,
        name=args.name,
    )
    print(connection_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
