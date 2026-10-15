import os
from flask import Flask, jsonify
from sqlalchemy import text
from app.config import config_by_name
from app.extensions import db, migrate, csrf
from app.cli import register_commands


from werkzeug.middleware.proxy_fix import ProxyFix


def create_app(config_name: str = "default") -> Flask:
    """Flask Application Factory."""
    app = Flask(__name__, instance_relative_config=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Determine configuration class
    if isinstance(config_name, str):
        config_class = config_by_name.get(config_name, config_by_name["default"])
    else:
        config_class = config_name

    app.config.from_object(config_class)
    if hasattr(config_class, "init_app"):
        config_class.init_app(app)

    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    # Register CLI commands
    register_commands(app)

    # Register blueprints
    from app.blueprints.public.routes import public_bp
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.webhooks.routes import webhooks_bp
    from app.blueprints.auth.routes import auth_bp
    from app.blueprints.clinic.routes import clinic_bp

    # External webhooks verify HMAC signatures instead of browser CSRF tokens
    csrf.exempt(webhooks_bp)

    app.register_blueprint(public_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(clinic_bp)

    # Root redirect to demo tenant booking page or dashboard
    @app.route("/")
    def index():
        from flask import redirect, url_for
        return redirect(url_for("public.booking_page", slug="pondy-arena"))

    # Health check route
    @app.route("/healthz")
    def health_check():
        try:
            db.session.execute(text("SELECT 1"))
            db_status = "ok"
        except Exception as exc:
            app.logger.error("Health check DB ping failed: %s", exc)
            return jsonify({"status": "degraded", "db": "unreachable"}), 503

        return jsonify({
            "status": "ok",
            "db": db_status,
            "service": "SlotEngine",
            "env": app.config.get("ENV", "production" if not app.config.get("DEBUG") else "development"),
        }), 200

    return app
