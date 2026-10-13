# Project Rules & Architecture Invariants (SlotEngine Multi-Tenant SaaS)

You are building **SlotEngine**, a multi-tenant booking, queue, and billing SaaS built for two verticals ("One Engine, Two Skins"):

1. `TURF`: Sports turfs, badminton courts, pickleball courts, and clubs.
2. `CLINIC`: Small medical clinics, dental practices, physiotherapy, and Ayurveda centres.

Always read `@[PRD](docs/PRD.md)` and `@[TRD](docs/TRD.md)` before planning or modifying database models, routes, or templates.

## 1. Tech Stack Constraints (Strict)

- **Language & Framework:** Python 3.12+, Flask 3.x (Application Factory pattern with Blueprints).
- **Database & ORM:** PostgreSQL 16+ in production (SQLite only if explicitly running isolated unit tests, but prefer Postgres features), SQLAlchemy 2.0 (`Mapped`, `mapped_column`), Alembic / Flask-Migrate.
- **Frontend:** Server-Side Rendered Jinja2 templates + **HTMX 2.x** + **Tailwind CSS** (via standalone CLI or CDN in dev). NO React, Vue, or complex SPA state.
- **Mobile-First Requirement:** 95% of end-customers open `/b/<tenant_slug>` inside the WhatsApp in-app mobile browser. Touch targets must be at least 44x44px, payloads under 100KB, and checkout completed in under 3 taps.

## 2. Multi-Tenancy & Security Invariants

- **Tenant Isolation:** Every business table (`users`, `resources`, `availability_rules`, `customers`, `bookings`, `payments`, `patients`, `visits`, `invoices`, `notification_logs`) MUST include `tenant_id UUID NOT NULL REFERENCES tenants(id)`.
- **Query Scoping:** NEVER write a SQLAlchemy query on a tenant-scoped table without explicitly filtering by `tenant_id`.
- **RBAC Roles:** Enforce role decorators (`@require_role('OWNER', 'DOCTOR', 'RECEPTIONIST', 'STAFF')`):
  - In `CLINIC` tenants, `RECEPTIONIST` can manage bookings, walk-in tokens, and invoices, but CANNOT view or edit clinical notes (`visits.clinical_notes`).
- **Gateway Secrets:** Store per-tenant Razorpay secrets encrypted at rest using `cryptography.fernet.Fernet` keyed by `APP_MASTER_KEY`.
- **Webhook Verification:** NEVER mark a booking as paid from a browser redirect alone. Always verify `X-Razorpay-Signature` using HMAC-SHA256 on raw request bytes and handle webhooks idempotently.

## 3. Concurrency & Zero Double-Booking Rules

- All timestamps must be stored in UTC (`TIMESTAMPTZ`) and converted to `Asia/Kolkata` (`IST`) at the presentation layer.
- When initiating a booking hold, wrap the check-and-insert inside a database transaction (`SELECT ... FOR UPDATE`).
- A slot `(tenant_id, resource_id, slot_date, start_time)` is unavailable if an existing row has:
  - `status IN ('CONFIRMED', 'BLOCKED')`, OR
  - `status = 'PENDING_PAYMENT' AND locked_until > NOW()`
- Never rely solely on a background job to free expired holds; queries must evaluate `locked_until > NOW()` dynamically.

## 4. Verification & Testing Protocol

- Write `pytest` tests for every booking concurrency edge case, pricing rule, and role permission.
- After implementing UI changes, use the Antigravity browser tool to verify mobile viewport (`390x844`) rendering and HTMX partial swaps without console errors.
