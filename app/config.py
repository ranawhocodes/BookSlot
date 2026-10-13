import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


class Config:
    """Base configuration with shared defaults."""
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-slotengine-secret-key-change-in-prod")
    APP_MASTER_KEY = os.environ.get("APP_MASTER_KEY", "c2xvdGVuZ2luZS1tYXN0ZXIta2V5LTMyYnl0ZXM=")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")


class DevelopmentConfig(Config):
    """Development environment configuration."""
    DEBUG = True
    # Default to local SQLite dev db if PostgreSQL URL is not provided in .env
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'instance' / 'slotengine_dev.db'}"
    )


class TestingConfig(Config):
    """Test environment configuration."""
    TESTING = True
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    WTF_CSRF_ENABLED = False


class ProductionConfig(Config):
    """Production environment configuration."""
    DEBUG = False
    TESTING = False
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")


config_by_name = {
    "dev": DevelopmentConfig,
    "development": DevelopmentConfig,
    "test": TestingConfig,
    "testing": TestingConfig,
    "prod": ProductionConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
