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

    # Health check route
    @app.route("/healthz")
    def health_check():
        return jsonify({
            "status": "ok",
            "service": "SlotEngine",
            "env": app.config.get("ENV", "production" if not app.config.get("DEBUG") else "development")
        }), 200

    return app
