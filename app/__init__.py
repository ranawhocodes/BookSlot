import os
from flask import Flask, jsonify
from app.config import config_by_name
from app.extensions import db, migrate
from app.cli import register_commands


def create_app(config_name: str = "default") -> Flask:
    """Flask Application Factory."""
    app = Flask(__name__, instance_relative_config=True)

    # Determine configuration class
    if isinstance(config_name, str):
        config_class = config_by_name.get(config_name, config_by_name["default"])
    else:
        config_class = config_name

    app.config.from_object(config_class)

    # Ensure instance folder exists
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass

    # Initialize extensions
    db.init_app(app)
    migrate.init_app(app, db)

    # Register CLI commands
    register_commands(app)

    # Register blueprints
    from app.blueprints.public.routes import public_bp
    from app.blueprints.dashboard.routes import dashboard_bp
    from app.blueprints.webhooks.routes import webhooks_bp
    from app.blueprints.auth.routes import auth_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(webhooks_bp)
    app.register_blueprint(auth_bp)

    # Root redirect to demo tenant booking page or dashboard
    @app.route("/")
    def index():
        from flask import redirect, url_for
        return redirect(url_for("public.booking_page", slug="pondy-arena"))

    # Health check route
    @app.route("/healthz")
    def health_check():
        return jsonify({
            "status": "ok",
            "service": "SlotEngine",
            "env": app.config.get("ENV", "production" if not app.config.get("DEBUG") else "development")
        }), 200

    return app
