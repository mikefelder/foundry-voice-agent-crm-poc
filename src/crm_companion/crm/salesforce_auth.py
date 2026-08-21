"""Salesforce credential acquisition.

Two providers for two environments:

* ``SfCliTokenProvider`` borrows the session the ``sf`` CLI already holds. Local
  development only - it shells out and depends on a developer's machine state.
* ``JwtTokenProvider`` performs the OAuth 2.0 JWT bearer flow against a Connected
  App. No browser, no callback, no user interaction, so it is what runs deployed.

Neither ever logs a token. ``Credentials`` deliberately has no ``__repr__``
exposing the secret, because tokens leak through exception tracebacks and debug
logging far more often than through deliberate prints.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Protocol

import httpx
import jwt

from crm_companion.config import ConfigError, Settings

__all__ = [
    "AuthError",
    "Credentials",
    "JwtTokenProvider",
    "SfCliTokenProvider",
    "TokenProvider",
    "build_token_provider",
]

JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Assertion lifetime. Salesforce rejects anything beyond a few minutes.
_ASSERTION_TTL_SECONDS = 180

# Salesforce does not return expires_in for this grant, so we re-auth on a
# conservative schedule and additionally on any 401 from the API.
_ASSUMED_SESSION_TTL_SECONDS = 30 * 60

_ORG_ALIAS = re.compile(r"\A[A-Za-z0-9._@-]{1,64}\Z")


class AuthError(RuntimeError):
    """Raised when credentials cannot be obtained."""


@dataclass(frozen=True)
class Credentials:
    instance_url: str
    access_token: str = field(repr=False)

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


class TokenProvider(Protocol):
    async def get(self) -> Credentials: ...

    async def refresh(self) -> Credentials:
        """Discard any cached token and acquire a new one."""


class SfCliTokenProvider:
    """Borrows the sf CLI's existing session. Local development only."""

    def __init__(self, org_alias: str) -> None:
        if not _ORG_ALIAS.match(org_alias):
            raise AuthError(f"Invalid org alias: {org_alias!r}")
        self._alias = org_alias
        self._cached: Credentials | None = None

    async def get(self) -> Credentials:
        if self._cached is None:
            self._cached = await self._load()
        return self._cached

    async def refresh(self) -> Credentials:
        self._cached = None
        return await self.get()

    async def _load(self) -> Credentials:
        # Recent CLI versions redact accessToken in `org display` output and
        # direct callers to `org auth show-access-token`, so the instance URL
        # and the token come from two different commands.
        instance_url, access_token = await asyncio.gather(
            self._sf_json(("org", "display"), "instanceUrl"),
            self._sf_json(("org", "auth", "show-access-token"), "accessToken"),
        )
        if access_token.startswith("[REDACTED"):
            raise AuthError("sf returned a redacted access token. Upgrade the CLI or use JWT auth.")
        return Credentials(instance_url=instance_url, access_token=access_token)

    async def _sf_json(self, command: tuple[str, ...], key: str) -> str:
        # No shell: arguments are passed as a list so the alias cannot inject.
        proc = await asyncio.create_subprocess_exec(
            "sf",
            *command,
            "--target-org",
            self._alias,
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:200]
            raise AuthError(f"sf {' '.join(command)} failed for {self._alias!r}: {detail}")

        try:
            return json.loads(stdout)["result"][key]
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthError(f"Could not read {key} from sf {' '.join(command)}") from exc


class JwtTokenProvider:
    """OAuth 2.0 JWT bearer flow against a Connected App."""

    def __init__(
        self,
        *,
        login_url: str,
        client_id: str,
        username: str,
        private_key: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._login_url = login_url.rstrip("/")
        self._client_id = client_id
        self._username = username
        self._private_key = private_key
        self._http = http
        self._cached: Credentials | None = None
        self._expires_at = 0.0

    async def get(self) -> Credentials:
        if self._cached is not None and time.monotonic() < self._expires_at:
            return self._cached
        return await self.refresh()

    async def refresh(self) -> Credentials:
        assertion = self._build_assertion()
        payload = {"grant_type": JWT_BEARER_GRANT, "assertion": assertion}

        if self._http is not None:
            response = await self._http.post(
                f"{self._login_url}/services/oauth2/token", data=payload
            )
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._login_url}/services/oauth2/token", data=payload
                )

        if response.status_code != 200:
            raise AuthError(self._describe_failure(response))

        body = response.json()
        self._cached = Credentials(
            instance_url=body["instance_url"],
            access_token=body["access_token"],
        )
        self._expires_at = time.monotonic() + _ASSUMED_SESSION_TTL_SECONDS
        return self._cached

    def _build_assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._client_id,
            "sub": self._username,
            "aud": self._login_url,
            "exp": now + _ASSERTION_TTL_SECONDS,
        }
        return jwt.encode(claims, self._private_key, algorithm="RS256")

    @staticmethod
    def _describe_failure(response: httpx.Response) -> str:
        try:
            body = response.json()
            error = body.get("error", "")
            description = body.get("error_description", "")
        except ValueError:
            return f"JWT bearer flow failed with HTTP {response.status_code}"

        hint = ""
        if error == "invalid_grant":
            # By far the most common first-run failures, and the message alone
            # points at none of them.
            hint = (
                " Common causes: the Connected App has not finished propagating "
                "(allow 2-10 minutes), the user is not pre-authorized under "
                "Manage > Edit Policies, the certificate does not match the "
                "signing key, or the username or audience is wrong."
            )
        return f"JWT bearer flow rejected: {error}: {description}.{hint}"


def build_token_provider(settings: Settings) -> TokenProvider:
    """Prefer a fully configured JWT setup; fall back to the sf CLI session."""
    try:
        settings.require_salesforce_jwt()
    except ConfigError:
        if settings.sf_org_alias:
            return SfCliTokenProvider(settings.sf_org_alias)
        raise

    return JwtTokenProvider(
        login_url=settings.sf_login_url,
        client_id=settings.sf_client_id or "",
        username=settings.sf_username or "",
        private_key=settings.load_private_key(),
    )
