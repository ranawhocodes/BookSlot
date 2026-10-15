import os
import pytest
from app import create_app
from app.config import ProductionConfig, Config, INSECURE_SECRET_DEFAULTS


def test_production_config_fails_without_postgres(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key-prod-1234567890")
    monkeypatch.setenv("APP_MASTER_KEY", "a" * 44)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "prod_webhook_secret_98765")

    with pytest.raises(RuntimeError, match="CRITICAL: Production environment requires a valid PostgreSQL DATABASE_URL"):
        create_app("production")


def test_production_config_fails_with_sqlite_in_prod(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///prod.db")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key-prod-1234567890")
    monkeypatch.setenv("APP_MASTER_KEY", "a" * 44)
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "prod_webhook_secret_98765")

    with pytest.raises(RuntimeError, match="CRITICAL: Production environment requires a valid PostgreSQL DATABASE_URL"):
        create_app("production")


@pytest.mark.parametrize("insecure_secret", [
    "",
    "dev-slotengine-secret-key-change-in-prod",
    "replace-with-strong-secret-key",
])
def test_production_config_fails_with_insecure_secret_key(monkeypatch, insecure_secret):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/slotengine_prod")
    monkeypatch.setenv("SECRET_KEY", insecure_secret)
    monkeypatch.setenv("APP_MASTER_KEY", "valid-master-key-32-bytes-long-123456789012")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "prod_webhook_secret_98765")

    with pytest.raises(RuntimeError, match="CRITICAL: Refusing to start production with missing or default SECRET_KEY"):
        create_app("production")


def test_production_config_fails_with_default_app_master_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/slotengine_prod")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key-prod-1234567890")
    monkeypatch.setenv("APP_MASTER_KEY", "c2xvdGVuZ2luZS1tYXN0ZXIta2V5LTMyYnl0ZXM=")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "prod_webhook_secret_98765")

    with pytest.raises(RuntimeError, match="CRITICAL: Refusing to start production with missing or default APP_MASTER_KEY"):
        create_app("production")


def test_production_config_fails_with_default_razorpay_webhook_secret(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/slotengine_prod")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key-prod-1234567890")
    monkeypatch.setenv("APP_MASTER_KEY", "valid-master-key-32-bytes-long-123456789012")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "dev_razorpay_secret_change_in_prod")

    with pytest.raises(RuntimeError, match="CRITICAL: Refusing to start production with missing or default RAZORPAY_WEBHOOK_SECRET"):
        create_app("production")


def test_cookie_security_defaults():
    assert Config.SESSION_COOKIE_HTTPONLY is True
    assert Config.SESSION_COOKIE_SAMESITE == "Lax"
    assert ProductionConfig.SESSION_COOKIE_SECURE is True


def test_seed_demo_command_blocked_in_production(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/slotengine_prod")
    monkeypatch.setenv("SECRET_KEY", "super-secret-key-prod-1234567890")
    monkeypatch.setenv("APP_MASTER_KEY", "valid-master-key-32-bytes-long-123456789012")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "prod_webhook_secret_98765")

    prod_app = create_app("production")
    runner = prod_app.test_cli_runner()
    with prod_app.app_context():
        result = runner.invoke(args=["seed-demo"])
        assert result.exit_code != 0
        assert "Refusing to seed demo accounts with default passwords in production" in result.output
