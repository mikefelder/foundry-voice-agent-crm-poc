from pathlib import Path

import pytest

from crm_companion.config import ConfigError, Settings


def _settings(**overrides) -> Settings:
    # _env_file=None keeps a developer's real .env out of the test run.
    return Settings(_env_file=None, **overrides)


class TestDefaults:
    def test_defaults_to_the_offline_provider(self):
        assert _settings().crm_provider == "fake"
        assert _settings().use_salesforce is False

    def test_no_credentials_required_to_construct(self):
        _settings()  # must not raise


class TestApiVersionNormalisation:
    @pytest.mark.parametrize(("raw", "expected"), [("62.0", "v62.0"), ("v62.0", "v62.0")])
    def test_leading_v_is_added_once(self, raw, expected):
        assert _settings(sf_api_version=raw).sf_api_version == expected


class TestSalesforceGuard:
    def test_missing_everything_names_every_variable(self):
        with pytest.raises(ConfigError) as err:
            _settings().require_salesforce_jwt()
        message = str(err.value)
        assert "SF_CLIENT_ID" in message
        assert "SF_USERNAME" in message
        assert "SF_PRIVATE_KEY" in message

    def test_inline_key_satisfies_the_key_requirement(self):
        _settings(
            sf_client_id="abc",
            sf_username="user@example.com",
            sf_private_key="-----BEGIN PRIVATE KEY-----",
        ).require_salesforce_jwt()

    def test_key_path_satisfies_the_key_requirement(self):
        _settings(
            sf_client_id="abc",
            sf_username="user@example.com",
            sf_private_key_path=Path("/nonexistent/server.key"),
        ).require_salesforce_jwt()


class TestPrivateKeyLoading:
    def test_inline_key_wins_over_path(self, tmp_path):
        key_file = tmp_path / "server.key"
        key_file.write_text("from-file")
        settings = _settings(sf_private_key="from-env", sf_private_key_path=key_file)
        assert settings.load_private_key() == "from-env"

    def test_reads_from_path(self, tmp_path):
        key_file = tmp_path / "server.key"
        key_file.write_text("from-file")
        assert _settings(sf_private_key_path=key_file).load_private_key() == "from-file"

    def test_missing_file_reports_the_path(self, tmp_path):
        missing = tmp_path / "absent.key"
        with pytest.raises(ConfigError, match="does not exist"):
            _settings(sf_private_key_path=missing).load_private_key()

    def test_no_key_configured(self):
        with pytest.raises(ConfigError, match="No Salesforce signing key"):
            _settings().load_private_key()


class TestOtherGuards:
    def test_foundry_guard_names_missing_variables(self):
        with pytest.raises(ConfigError, match="PROJECT_ENDPOINT"):
            _settings().require_foundry()

    def test_voicelive_guard_mentions_version_pinning(self):
        with pytest.raises(ConfigError, match="Pin the API version"):
            _settings().require_voicelive()

    def test_guards_pass_when_configured(self):
        _settings(project_endpoint="https://x", project_name="p").require_foundry()
        _settings(
            voicelive_endpoint="https://x", voicelive_api_version="2026-01-01"
        ).require_voicelive()


class TestSecretHandling:
    def test_secrets_are_not_exposed_by_repr(self):
        settings = _settings(sf_private_key="super-secret", tool_api_key="also-secret")
        rendered = repr(settings)
        assert "super-secret" not in rendered
        assert "also-secret" not in rendered
