from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from sqlalchemy import select
from app.extensions import db
from app.models import User, Tenant

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        user = db.session.execute(
            select(User).filter_by(phone=phone, is_active=True)
        ).scalar_one_or_none()

        if user and user.check_password(password):
            session["user_id"] = str(user.id)
            session["tenant_id"] = str(user.tenant_id)
            session["user_role"] = user.role.value
            session["user_name"] = user.name
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(url_for("dashboard.turf_dashboard"))

        flash("Invalid phone or password.", "error")

    # Fetch available demo tenants for easy testing login
    tenants = db.session.execute(select(Tenant).filter_by(is_active=True)).scalars().all()
    return render_template("auth/login.html", tenants=tenants)


@auth_bp.route("/switch-tenant/<slug>")
def switch_tenant(slug: str):
    """Convenience endpoint to switch active tenant session for testing/demo."""
    tenant = db.session.execute(select(Tenant).filter_by(slug=slug)).scalar_one_or_none()
    if tenant:
        session["tenant_id"] = str(tenant.id)
        session["user_role"] = "OWNER"
        flash(f"Switched to {tenant.name}", "info")
    return redirect(url_for("dashboard.turf_dashboard", slug=slug))


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for("auth.login"))
