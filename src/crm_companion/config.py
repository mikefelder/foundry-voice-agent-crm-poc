"""Configuration.

Settings load permissively so an offline test run or prompt-tuning session needs
no Salesforce or Azure credentials at all. Each subsystem validates its own
requirements at the point of use via ``require_*``, which fails with a message
naming the missing variables rather than a stack trace deep in a client.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "ConfigError", "get_settings"]

ProviderName = Literal["salesforce", "fake"]


class ConfigError(RuntimeError):
    """Raised when a subsystem is used without the configuration it needs."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    crm_provider: ProviderName = "fake"

    # ---- Salesforce --------------------------------------------------------
    sf_login_url: str = "https://login.salesforce.com"
    sf_api_version: str = "v62.0"
    sf_client_id: str | None = None
    sf_username: str | None = None
    sf_private_key_path: Path | None = None
    sf_private_key: SecretStr | None = None
    sf_org_alias: str | None = None
    sf_instance_url: str | None = None
    sf_access_token: SecretStr | None = None

    # Custom field API names. Approximated for the POC org; point these at the
    # real names to run against a production org without touching code.
    sf_field_comments: str = "Comments__c"
    sf_field_customer_need: str = "Customer_Need__c"
    sf_field_idempotency: str = "Idempotency_Key__c"
    sf_ledger_object: str = "Voice_Write_Log__c"

    # Licences whose holders can actually receive a Chatter mention. An
    # allowlist because the failure directions are asymmetric: excluding a valid
    # user surfaces instantly as "can't find them", while including one who can
    # never be notified fails silently. Identity-licence users are the trap -
    # they look like ordinary Standard users in every other respect.
    sf_mention_licenses: str = "Salesforce,Salesforce Platform,Chatter Free,Chatter Only"

    # ---- Foundry project ---------------------------------------------------
    project_endpoint: str | None = None
    project_name: str | None = None
    model_deployment_name: str = "gpt-realtime"

    # ---- Voice Live --------------------------------------------------------
    voicelive_endpoint: str | None = None
    voicelive_api_version: str | None = None
    voice_name: str = "en-US-Ava:DragonHDLatestNeural"

    # ---- Agent -------------------------------------------------------------
    agent_name: str = "crm-sales-companion"
    agent_version: str | None = None
    conversation_id: str | None = None

    # ---- Tool API ----------------------------------------------------------
    tool_api_base_url: str | None = None
    tool_api_key: SecretStr | None = None

    # ---- Demo data ---------------------------------------------------------
    # Kept in configuration rather than source so no real names are committed and
    # the seed script runs against any org.
    demo_account_name: str = "Contoso Building Supply"
    demo_opportunity_name: str = "Northgate Commons Phase 2"
    demo_mention_name: str | None = None

    request_timeout_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _normalise_api_version(self) -> Settings:
        if not self.sf_api_version.startswith("v"):
            object.__setattr__(self, "sf_api_version", f"v{self.sf_api_version}")
        return self

    # ---- guards ------------------------------------------------------------

    @property
    def use_salesforce(self) -> bool:
        return self.crm_provider == "salesforce"

    @property
    def mention_licenses(self) -> tuple[str, ...]:
        return tuple(part.strip() for part in self.sf_mention_licenses.split(",") if part.strip())

    def require_salesforce_jwt(self) -> None:
        """Server-to-server path: Connected App consumer key plus a signing key."""
        missing = [
            name
            for name, value in (
                ("SF_CLIENT_ID", self.sf_client_id),
                ("SF_USERNAME", self.sf_username),
            )
            if not value
        ]
        if not (self.sf_private_key or self.sf_private_key_path):
            missing.append("SF_PRIVATE_KEY or SF_PRIVATE_KEY_PATH")
        if missing:
            raise ConfigError(f"Salesforce JWT auth needs: {', '.join(missing)}")

    def require_foundry(self) -> None:
        missing = [
            name
            for name, value in (
                ("PROJECT_ENDPOINT", self.project_endpoint),
                ("PROJECT_NAME", self.project_name),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"Foundry access needs: {', '.join(missing)}")

    def require_voicelive(self) -> None:
        missing = [
            name
            for name, value in (
                ("VOICELIVE_ENDPOINT", self.voicelive_endpoint),
                ("VOICELIVE_API_VERSION", self.voicelive_api_version),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"Voice Live needs: {', '.join(missing)}. "
                "Pin the API version against the installed azure-ai-voicelive build."
            )

    def load_private_key(self) -> str:
        """Inline PEM wins over a path so deployments can inject from Key Vault."""
        if self.sf_private_key:
            return self.sf_private_key.get_secret_value()
        if self.sf_private_key_path:
            path = self.sf_private_key_path
            if not path.is_file():
                raise ConfigError(f"SF_PRIVATE_KEY_PATH does not exist: {path}")
            return path.read_text(encoding="utf-8")
        raise ConfigError("No Salesforce signing key configured")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
