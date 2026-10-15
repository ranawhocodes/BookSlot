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
            # Check for next parameter (from query string or form)
            next_url = request.args.get("next") or request.form.get("next")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return redirect(next_url)

            # Redirect based on tenant vertical
            tenant = db.session.get(Tenant, user.tenant_id)
            if tenant and tenant.vertical.value == "CLINIC":
                return redirect(url_for("clinic.queue_board", slug=tenant.slug))
            return redirect(url_for("dashboard.dashboard_home", slug=tenant.slug if tenant else None))

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
        session["user_name"] = f"{tenant.name} Owner"
        flash(f"Switched to {tenant.name}", "info")
        if tenant.vertical.value == "CLINIC":
            return redirect(url_for("clinic.queue_board", slug=tenant.slug))
        return redirect(url_for("dashboard.dashboard_home", slug=slug))
    return redirect(url_for("auth.login"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for("auth.login"))
