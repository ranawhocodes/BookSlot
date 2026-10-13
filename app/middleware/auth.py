from functools import wraps
from flask import session, redirect, url_for, flash, abort, current_app
from app.models import UserRole


def require_role(*allowed_roles: UserRole):
    """
    Decorator to enforce RBAC roles on routes.
    If no user session exists and app is in DEBUG/DEV mode, defaults to OWNER.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user_role = session.get("user_role")

            # Dev convenience: default to OWNER role if session not set in dev
            if not user_role and current_app.config.get("DEBUG"):
                user_role = UserRole.OWNER.value

            if not user_role:
                flash("Please log in to access this area.", "warning")
                return redirect(url_for("auth.login"))

            allowed_values = [r.value if isinstance(r, UserRole) else r for r in allowed_roles]
            if user_role not in allowed_values:
                abort(403, description="You do not have permission to view this resource.")

            return fn(*args, **kwargs)
        return wrapper
    return decorator
