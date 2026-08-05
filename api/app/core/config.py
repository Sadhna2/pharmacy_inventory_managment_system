from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://pharmacy:pharmacy@localhost:5432/pharmacy"

    secret_key: str = "dev-only-change-me-in-production"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    algorithm: str = "HS256"

    env: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"

    # Invoice intake (app/ai/intake). The only outbound call the system makes,
    # and the only feature that degrades rather than fails without config: with
    # no key the endpoint returns 503 and the rest of the API is unaffected.
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    #: Replay extractions from this directory instead of calling the API.
    #: Set for demos and for tests — a conference network is not a dependency
    #: worth having, and CI must not need a key or make network calls.
    intake_fixture_dir: str = ""

    # The registered identity of the firm doing the selling, printed on every
    # tax invoice. Deliberately configuration rather than a warehouse column:
    # a branch is a place, and it is the business that holds the registration.
    #
    # There is no default GSTIN, and there must not be. Rule 46(b) makes the
    # supplier's GSTIN a mandatory particular, so a document headed TAX INVOICE
    # without one is not a tax invoice — the buyer cannot claim input credit
    # against it. A placeholder would produce a document that looks valid and
    # is not, which is worse than refusing to produce one at all.
    seller_legal_name: str = ""
    seller_gstin: str = ""
    seller_address: str = ""

    @property
    def can_issue_tax_invoice(self) -> bool:
        """Whether a document may lawfully be captioned TAX INVOICE."""
        return bool(self.seller_legal_name and self.seller_gstin)

    @property
    def invoice_intake_enabled(self) -> bool:
        return bool(self.gemini_api_key or self.intake_fixture_dir)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
