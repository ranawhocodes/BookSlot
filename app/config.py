# app/config.py
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def normalize_db_url(raw_url: str | None, default_sqlite_path: Path | None = None) -> str:
    """
    Normalizes cloud Postgres URLs (postgres:// or postgresql://) to use psycopg v3 driver
    (postgresql+psycopg://) required by SQLAlchemy 2.0.
    """
    if not raw_url:
        if default_sqlite_path:
            return f"sqlite:///{default_sqlite_path}"
        return "sqlite:///:memory:"

    url = raw_url.strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://") and "+psycopg" not in url and "+psycopg2" not in url:
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Config:
    """Base configuration with shared defaults."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-slotengine-secret-key-change-in-prod")
    APP_MASTER_KEY = os.environ.get("APP_MASTER_KEY", "c2xvdGVuZ2luZS1tYXN0ZXIta2V5LTMyYnl0ZXM=")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")

    # Payment & Webhook Config
    RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
    RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
    RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "dev_razorpay_secret_change_in_prod")
    DEFAULT_MERCHANT_UPI_VPA = os.environ.get("DEFAULT_MERCHANT_UPI_VPA", "pondyarena@okaxis")

    # WhatsApp & Background Worker Config
    META_WA_API_TOKEN = os.environ.get("META_WA_API_TOKEN", "")
    META_WA_PHONE_NUMBER_ID = os.environ.get("META_WA_PHONE_NUMBER_ID", "")
    AISENSY_API_KEY = os.environ.get("AISENSY_API_KEY", "")
    WHATSAPP_LIVE_HTTP = os.environ.get("WHATSAPP_LIVE_HTTP", "false").lower() == "true"
    ENABLE_BACKGROUND_SCHEDULER = os.environ.get("ENABLE_BACKGROUND_SCHEDULER", "false").lower() == "true"


class DevelopmentConfig(Config):
    """Development environment configuration."""
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = normalize_db_url(
        os.environ.get("DATABASE_URL"),
        default_sqlite_path=BASE_DIR / "instance" / "slotengine_dev.db",
    )


class TestingConfig(Config):
    """Test environment configuration."""
    TESTING = True
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = normalize_db_url(os.environ.get("TEST_DATABASE_URL"))
    WTF_CSRF_ENABLED = False


class ProductionConfig(Config):
    """Production cloud environment configuration (Managed PostgreSQL 16+)."""
    DEBUG = False
    TESTING = False
    SQLALCHEMY_DATABASE_URI = normalize_db_url(
        os.environ.get("DATABASE_URL"),
        default_sqlite_path=BASE_DIR / "instance" / "slotengine_prod.db",
    )
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": int(os.environ.get("DB_POOL_SIZE", 5)),
        "max_overflow": int(os.environ.get("DB_MAX_OVERFLOW", 10)),
    }


config_by_name = {
    "dev": DevelopmentConfig,
    "development": DevelopmentConfig,
    "test": TestingConfig,
    "testing": TestingConfig,
    "prod": ProductionConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}