from typing import Optional
from flask import abort, request, session
from sqlalchemy import select
from app.extensions import db
from app.models import Tenant


def get_tenant_by_slug(slug: str) -> Tenant:
    """Fetches an active tenant by its URL slug or raises HTTP 404."""
    tenant = db.session.execute(
        select(Tenant).filter(
            Tenant.slug == slug,
            Tenant.is_active.is_(True)
        )
    ).scalar_one_or_none()

    if not tenant:
        abort(404, description=f"Tenant '{slug}' not found or inactive.")
    return tenant


def resolve_current_tenant() -> Optional[Tenant]:
    """
    Resolves the current tenant for dashboard or API operations.
    Checks:
    1. Query param 'slug' or form param 'slug'
    2. Session 'tenant_id'
    3. If resource_id is in form/args, derives tenant from the resource
    4. First active tenant as fallback
    """
    import uuid
    from app.models import Resource

    # 1. Query parameter or Form parameter
    slug = request.args.get("slug") or (request.form.get("slug") if request.form else None)
    if slug:
        return db.session.execute(
            select(Tenant).filter_by(slug=slug, is_active=True)
        ).scalar_one_or_none()

    # 2. Session
    tenant_id = session.get("tenant_id")
    if tenant_id:
        return db.session.execute(
            select(Tenant).filter_by(id=tenant_id, is_active=True)
        ).scalar_one_or_none()

    # 3. Derive from resource_id if present in form or args
    res_id_str = (request.form.get("resource_id") if request.form else None) or request.args.get("resource_id")
    if res_id_str:
        try:
            res_uuid = uuid.UUID(res_id_str)
            resource = db.session.execute(
                select(Resource).filter_by(id=res_uuid)
            ).scalar_one_or_none()
            if resource:
                return resource.tenant
        except (ValueError, TypeError):
            pass

    # 4. Check for 'pondy-arena' demo tenant or first active tenant
    demo = db.session.execute(
        select(Tenant).filter_by(slug="pondy-arena", is_active=True)
    ).scalar_one_or_none()
    if demo:
        return demo

    return db.session.execute(
        select(Tenant).filter_by(is_active=True)
    ).scalars().first()
