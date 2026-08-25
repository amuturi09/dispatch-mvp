"""
Unit tests for configuration loading.

Focus areas:
  - the postgres:// -> postgresql:// scheme normalization (Supabase/Railway hand
    out the legacy prefix, which SQLAlchemy 2.x rejects),
  - the cross-field safety checks that refuse dangerous misconfigurations.
"""

import pytest

from config import load_config, ConfigError


# All env vars load_config consults -- cleared before each test so the host
# environment can't leak in and make results non-deterministic.
_CONFIG_ENV_VARS = [
    "APP_ENV",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SIGNING_SECRET",
    "RETELL_API_KEY", "RETELL_AGENT_ID", "RETELL_SIP_DOMAIN",
    "DATABASE_URL", "PUBLIC_BASE_URL", "ADMIN_AUTH_TOKEN",
    "ALLOW_TEST_MODE_BILLING",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- postgres scheme normalization -----------------------------------------

def test_legacy_postgres_scheme_is_rewritten(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@host:5432/db")
    cfg = load_config(require_all=False)
    assert cfg.database_url == "postgresql://user:pw@host:5432/db"


def test_modern_postgresql_scheme_left_unchanged(monkeypatch):
    url = "postgresql://user:pw@host:5432/db"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = load_config(require_all=False)
    assert cfg.database_url == url


def test_only_the_prefix_is_replaced(monkeypatch):
    # A password containing the substring "postgres://" must not be mangled.
    monkeypatch.setenv("DATABASE_URL", "postgres://u:postgres://x@host/db")
    cfg = load_config(require_all=False)
    assert cfg.database_url == "postgresql://u:postgres://x@host/db"


def test_default_database_url_is_sqlite(monkeypatch):
    cfg = load_config(require_all=False)
    assert cfg.database_url == "sqlite:///./dispatch_mvp.db"


# --- cross-field safety checks ---------------------------------------------

def test_live_stripe_key_outside_production_is_rejected(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_abc123")
    with pytest.raises(ConfigError, match="live Stripe key"):
        load_config(require_all=False)


def test_production_requires_https_base_url(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://not-secure.example.com")
    with pytest.raises(ConfigError, match="https"):
        load_config(require_all=False)


def test_require_all_raises_when_credentials_missing(monkeypatch):
    with pytest.raises(ConfigError):
        load_config(require_all=True)


def test_dev_mode_loads_without_credentials(monkeypatch):
    cfg = load_config(require_all=False)
    assert cfg.env == "development"
    assert cfg.admin_auth_token is None  # unset -> admin endpoints open in dev
