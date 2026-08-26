"""
Central configuration. Every credential the system needs comes from here --
no module should read os.getenv() directly anywhere else.

Fails fast at startup if a required variable is missing for the mode you're
running in, instead of failing confusingly the first time a webhook fires.
"""

from __future__ import annotations
import os
import sys
from dataclasses import dataclass, field

# Load a local .env if present so `cp .env.example .env` + edit is all that's
# needed for local runs. Optional dependency: if python-dotenv isn't installed
# (e.g. a minimal prod image that injects env vars directly), this is a no-op.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


class ConfigError(RuntimeError):
    pass


def _env(key: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.getenv(key, default)
    if required and not val:
        raise ConfigError(
            f"Missing required environment variable: {key}. "
            f"See .env.example / CREDENTIALS_SETUP.md for how to obtain it."
        )
    return val


@dataclass
class TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str  # the vanity/toll-free number callers dial


@dataclass
class StripeConfig:
    secret_key: str
    webhook_signing_secret: str  # from Stripe CLI or Dashboard, per-endpoint
    is_live: bool = field(init=False)

    def __post_init__(self):
        self.is_live = self.secret_key.startswith("sk_live_")


@dataclass
class RetellConfig:
    api_key: str  # must be the "webhook badge" key from the Retell dashboard
    agent_id: str | None = None
    sip_domain: str | None = None  # Retell SIP host; fixed value sip.retellai.com, kept configurable via RETELL_SIP_DOMAIN


@dataclass
class AppConfig:
    env: str
    base_url: str  # public HTTPS URL of this service, used to build webhook URLs
    database_url: str
    twilio: TwilioConfig
    stripe: StripeConfig
    retell: RetellConfig
    admin_auth_token: str | None  # if None, admin endpoints are unprotected (dev mode)
    session_secret: str  # HMAC key that signs contractor session tokens (partner login)
    allow_test_mode_billing: bool  # if True, refuses to let a live Stripe key run without this being explicitly false


def load_config(require_all: bool = True) -> AppConfig:
    """
    require_all=True (default): raises ConfigError immediately if anything
    needed for a real deployment is missing -- call this at process startup.

    require_all=False: best-effort load for local/dev use (e.g. running
    core/test_flow.py, which needs none of this).
    """
    env = _env("APP_ENV", "development")

    try:
        twilio = TwilioConfig(
            account_sid=_env("TWILIO_ACCOUNT_SID", required=require_all) or "",
            auth_token=_env("TWILIO_AUTH_TOKEN", required=require_all) or "",
            from_number=_env("TWILIO_FROM_NUMBER", required=require_all) or "",
        )
        stripe_cfg = StripeConfig(
            secret_key=_env("STRIPE_SECRET_KEY", required=require_all) or "",
            webhook_signing_secret=_env("STRIPE_WEBHOOK_SIGNING_SECRET", required=require_all) or "",
        )
        retell = RetellConfig(
            api_key=_env("RETELL_API_KEY", required=require_all) or "",
            agent_id=_env("RETELL_AGENT_ID", required=False),
            sip_domain=_env("RETELL_SIP_DOMAIN", required=require_all) or "",
        )
        database_url = _env("DATABASE_URL", "sqlite:///./dispatch_mvp.db", required=False)
        # SQLAlchemy requires the "postgresql://" scheme; some providers (Supabase,
        # Heroku, Railway) hand out URLs with the legacy "postgres://" prefix.
        if database_url and database_url.startswith("postgres://"):
            database_url = "postgresql://" + database_url[len("postgres://"):]
        base_url = _env("PUBLIC_BASE_URL", required=require_all) or ""
        admin_token = _env("ADMIN_AUTH_TOKEN", required=False)

        # Signs contractor session tokens. Required for real deployments; in dev
        # we fall back to a fixed, clearly-insecure value so partner login works
        # locally. Tokens signed with the dev key are invalid anywhere else.
        session_secret = _env("SESSION_SECRET", required=require_all)
        if not session_secret:
            session_secret = "dev-insecure-session-secret-change-me"
            print(
                "[CONFIG WARNING] SESSION_SECRET not set -- using an insecure dev key "
                "for contractor sessions. Set SESSION_SECRET before deploying.",
                file=sys.stderr,
            )

        cfg = AppConfig(
            env=env,
            base_url=base_url,
            database_url=database_url,
            twilio=twilio,
            stripe=stripe_cfg,
            retell=retell,
            admin_auth_token=admin_token,
            session_secret=session_secret,
            allow_test_mode_billing=_env("ALLOW_TEST_MODE_BILLING", "true").lower() == "true",
        )
    except ConfigError as e:
        print(f"[CONFIG ERROR] {e}", file=sys.stderr)
        raise

    # Cross-field sanity checks -- catch dangerous misconfigurations, not just missing values.
    if cfg.stripe.is_live and cfg.env != "production":
        raise ConfigError(
            "A live Stripe key (sk_live_...) is set but APP_ENV is not 'production'. "
            "Refusing to start -- this would risk real charges from a dev/staging environment."
        )
    if cfg.env == "production" and not cfg.base_url.startswith("https://"):
        raise ConfigError("PUBLIC_BASE_URL must be an https:// URL in production (Twilio/Stripe/Retell require TLS).")

    return cfg
