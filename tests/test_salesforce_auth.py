import json

import httpx
import jwt
import pytest

from crm_companion.config import ConfigError, Settings
from crm_companion.crm.salesforce_auth import (
    AuthError,
    Credentials,
    JwtTokenProvider,
    SfCliTokenProvider,
    build_token_provider,
)

# Throwaway key generated per-run; never a real credential.
_PRIVATE_KEY = None


def private_key() -> str:
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _PRIVATE_KEY = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    return _PRIVATE_KEY


class TestCredentials:
    def test_token_is_not_in_repr(self):
        creds = Credentials(instance_url="https://x", access_token="super-secret")
        assert "super-secret" not in repr(creds)

    def test_auth_header_format(self):
        creds = Credentials(instance_url="https://x", access_token="tok")
        assert creds.auth_header() == {"Authorization": "Bearer tok"}


class TestSfCliTokenProvider:
    @pytest.mark.parametrize("alias", ["dev org", "a;rm -rf /", "x" * 65, ""])
    def test_rejects_suspicious_aliases(self, alias):
        with pytest.raises(AuthError, match="Invalid org alias"):
            SfCliTokenProvider(alias)

    @pytest.mark.parametrize("alias", ["devorg", "my-org_1", "user@example.com"])
    def test_accepts_reasonable_aliases(self, alias):
        SfCliTokenProvider(alias)

    async def test_combines_two_commands(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            payload = (
                {"result": {"instanceUrl": "https://example.my.salesforce.com"}}
                if "display" in args
                else {"result": {"accessToken": "live-token"}}
            )
            return _FakeProc(json.dumps(payload).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        creds = await SfCliTokenProvider("devorg").get()
        assert creds.instance_url == "https://example.my.salesforce.com"
        assert creds.access_token == "live-token"

    async def test_detects_redacted_token(self, monkeypatch):
        """Recent CLI versions mask secrets; a redacted string must not be used as a token."""

        async def fake_exec(*args, **kwargs):
            payload = (
                {"result": {"instanceUrl": "https://x"}}
                if "display" in args
                else {"result": {"accessToken": "[REDACTED] Use 'sf org auth show-access-token'"}}
            )
            return _FakeProc(json.dumps(payload).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(AuthError, match="redacted"):
            await SfCliTokenProvider("devorg").get()

    async def test_surfaces_cli_failure(self, monkeypatch):
        async def fake_exec(*args, **kwargs):
            return _FakeProc(b"", stderr=b"No authorization information found", returncode=1)

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        with pytest.raises(AuthError, match="No authorization information"):
            await SfCliTokenProvider("devorg").get()

    async def test_caches_until_refresh(self, monkeypatch):
        calls = []

        async def fake_exec(*args, **kwargs):
            calls.append(args)
            payload = (
                {"result": {"instanceUrl": "https://x"}}
                if "display" in args
                else {"result": {"accessToken": "tok"}}
            )
            return _FakeProc(json.dumps(payload).encode())

        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
        provider = SfCliTokenProvider("devorg")
        await provider.get()
        await provider.get()
        assert len(calls) == 2  # one pair, not two
        await provider.refresh()
        assert len(calls) == 4


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


class TestJwtTokenProvider:
    def _provider(self, handler) -> JwtTokenProvider:
        return JwtTokenProvider(
            login_url="https://login.salesforce.com",
            client_id="consumer-key",
            username="user@example.com",
            private_key=private_key(),
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    async def test_assertion_claims_and_grant(self):
        captured = {}

        def handler(request):
            from urllib.parse import parse_qs

            captured.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(
                200,
                json={"access_token": "tok", "instance_url": "https://x.my.salesforce.com"},
            )

        creds = await self._provider(handler).get()
        assert creds.access_token == "tok"
        assert captured["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"

        claims = jwt.decode(
            captured["assertion"],
            options={"verify_signature": False, "verify_aud": False, "verify_exp": False},
        )
        assert claims["iss"] == "consumer-key"
        assert claims["sub"] == "user@example.com"
        assert claims["aud"] == "https://login.salesforce.com"
        assert "exp" in claims

    async def test_token_is_cached(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"access_token": "tok", "instance_url": "https://x"})

        provider = self._provider(handler)
        await provider.get()
        await provider.get()
        assert len(calls) == 1

    async def test_refresh_bypasses_cache(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"access_token": "tok", "instance_url": "https://x"})

        provider = self._provider(handler)
        await provider.get()
        await provider.refresh()
        assert len(calls) == 2

    async def test_invalid_grant_explains_common_causes(self):
        def handler(request):
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "user hasn't approved"},
            )

        with pytest.raises(AuthError) as err:
            await self._provider(handler).get()
        message = str(err.value)
        assert "pre-authorized" in message
        assert "2-10 minutes" in message


class TestProviderSelection:
    def test_prefers_jwt_when_fully_configured(self, tmp_path):
        key_file = tmp_path / "server.key"
        key_file.write_text(private_key())
        settings = Settings(
            _env_file=None,
            sf_client_id="cid",
            sf_username="user@example.com",
            sf_private_key_path=key_file,
            sf_org_alias="devorg",
        )
        assert isinstance(build_token_provider(settings), JwtTokenProvider)

    def test_falls_back_to_cli_when_jwt_incomplete(self):
        settings = Settings(_env_file=None, sf_org_alias="devorg")
        assert isinstance(build_token_provider(settings), SfCliTokenProvider)

    def test_raises_when_nothing_configured(self):
        with pytest.raises(ConfigError):
            build_token_provider(Settings(_env_file=None, sf_org_alias=None))
