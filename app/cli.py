# app/cli.py
import click
import uuid
from datetime import time
from flask import Flask, current_app
from sqlalchemy import select
from app.extensions import db
from app.models import (
    Tenant,
    VerticalType,
    User,
    UserRole,
    Resource,
    AvailabilityRule,
)


def register_commands(app: Flask) -> None:
    @app.cli.command("seed-demo")
    @click.option("--reset", is_flag=True, help="Reset existing demo tenants before seeding.")
    def seed_demo_command(reset: bool) -> None:
        """Seed demo tenants: Pondy Arena Turf and Sri Aurobindo Dental & Ayurveda."""
        click.echo("Seeding demo tenants for SlotEngine...")
        seed_demo_data(reset=reset)
        click.echo("Demo seeding completed successfully!")

    @app.cli.command("send-reminders")
    def send_reminders_command() -> None:
        """Scan upcoming bookings (24h/2h) and clinic follow-ups and send reminders idempotently."""
        from app.services.notifier import send_reminders

        click.echo("Scanning upcoming appointments and clinic follow-up schedules...")
        res = send_reminders()
        click.echo(
            f"Reminders dispatched: "
            f"24h={res['reminders_24h_sent']}, "
            f"2h={res['reminders_2h_sent']}, "
            f"Follow-ups={res['followups_sent']}, "
            f"Quota Exceeded={res['quota_exceeded_count']}"
        )

    @app.cli.command("expire-holds")
    def expire_holds_command() -> None:
        """Release abandoned 7-minute payment holds that have passed their locked_until timestamp."""
        from app.services.slot_engine import expire_stale_holds

        count = expire_stale_holds()
        click.echo(f"Expired stale holds cleaned: {count}")

    @app.cli.command("maintenance-tick")
    def maintenance_tick_command() -> None:
        """Run one pass of stale hold cleanup + automated WhatsApp reminders (ideal for system cron)."""
        from app.services.background_runner import run_maintenance_cycle

        stats = run_maintenance_cycle(current_app._get_current_object())
        click.echo(
            f"Maintenance tick complete: "
            f"Expired Holds={stats['expired_holds_cleaned']}, "
            f"24h={stats['reminders_24h_sent']}, "
            f"2h={stats['reminders_2h_sent']}, "
            f"Follow-ups={stats['followups_sent']}"
        )

    @app.cli.command("run-worker")
    @click.option("--interval", default=300, help="Interval in seconds between maintenance cycles (default 300s).")
    def run_worker_command(interval: int) -> None:
        """Run continuous background worker loop for reminders and stale hold cleanup."""
        import time as time_mod
        from app.services.background_runner import run_maintenance_cycle

        app_obj = current_app._get_current_object()
        click.echo(f"Starting SlotEngine background worker (interval={interval}s)...")
        while True:
            try:
                stats = run_maintenance_cycle(app_obj)
                click.echo(f"[Worker Tick] {stats}")
            except Exception as exc:
                click.echo(f"[Worker Error] {exc}", err=True)
            time_mod.sleep(interval)


def seed_demo_data(reset: bool = False) -> None:
    """Idempotently seeds demo tenants, resources, pricing rules, and users."""
    db.create_all()

    # 1. TURF TENANT
    turf_slug = "pondy-arena"
    turf_tenant = db.session.execute(
        select(Tenant).filter_by(slug=turf_slug)
    ).scalar_one_or_none()

    if turf_tenant and reset:
        click.echo(f"Resetting existing tenant '{turf_slug}'...")
        db.session.delete(turf_tenant)
        db.session.commit()
        turf_tenant = None

    if not turf_tenant:
        click.echo("Creating TURF tenant: 'Pondy Arena Turf' (slug: pondy-arena)...")
        turf_tenant = Tenant(
            id=uuid.uuid4(),
            slug=turf_slug,
            name="Pondy Arena Turf",
            vertical=VerticalType.TURF,
            phone="9876543210",
            address="ECR Main Road, Lawspet, Pondicherry - 605008",
            timezone="Asia/Kolkata",
            currency="INR",
            payment_mode_config="VENUE_ONLY",
            advance_percentage=30,
            monthly_msg_quota=500,
        )
        db.session.add(turf_tenant)
        db.session.flush()

        turf_owner = User(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="Arun Kumar",
            phone="9876543210",
            email="arun@pondyarena.com",
            role=UserRole.OWNER,
        )
        turf_owner.set_password("password123")
        db.session.add(turf_owner)

        turf_football = Resource(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="5v5 Football Turf",
            category="Football",
            slot_duration_mins=60,
            base_price_paise=80000,
        )
        badminton_1 = Resource(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="Badminton Court 1",
            category="Badminton",
            slot_duration_mins=60,
            base_price_paise=40000,
        )
        badminton_2 = Resource(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="Badminton Court 2",
            category="Badminton",
            slot_duration_mins=60,
            base_price_paise=40000,
        )
        db.session.add_all([turf_football, badminton_1, badminton_2])
        db.session.flush()

        turf_resources = [
            (turf_football, 80000, 120000, 140000),
            (badminton_1, 40000, 60000, 70000),
            (badminton_2, 40000, 60000, 70000),
        ]

        for res, base_p, peak_p, weekend_p in turf_resources:
            for day in range(0, 5):
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=turf_tenant.id,
                        resource_id=res.id,
                        day_of_week=day,
                        start_time=time(6, 0),
                        end_time=time(17, 0),
                        is_break=False,
                        price_override_paise=base_p,
                    )
                )
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=turf_tenant.id,
                        resource_id=res.id,
                        day_of_week=day,
                        start_time=time(17, 0),
                        end_time=time(23, 0),
                        is_break=False,
                        price_override_paise=peak_p,
                    )
                )
            for day in range(5, 7):
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=turf_tenant.id,
                        resource_id=res.id,
                        day_of_week=day,
                        start_time=time(6, 0),
                        end_time=time(23, 0),
                        is_break=False,
                        price_override_paise=weekend_p,
                    )
                )

        db.session.commit()
        click.echo("TURF tenant seeded successfully.")
    else:
        click.echo("TURF tenant 'pondy-arena' already exists.")

    # 2. CLINIC TENANT
    clinic_slug = "sri-clinic"
    clinic_tenant = db.session.execute(
        select(Tenant).filter_by(slug=clinic_slug)
    ).scalar_one_or_none()

    if clinic_tenant and reset:
        click.echo(f"Resetting existing tenant '{clinic_slug}'...")
        db.session.delete(clinic_tenant)
        db.session.commit()
        clinic_tenant = None

    if not clinic_tenant:
        click.echo("Creating CLINIC tenant: 'Sri Aurobindo Dental & Ayurveda' (slug: sri-clinic)...")
        clinic_tenant = Tenant(
            id=uuid.uuid4(),
            slug=clinic_slug,
            name="Sri Aurobindo Dental & Ayurveda",
            vertical=VerticalType.CLINIC,
            phone="9876543220",
            address="Mission Street, Heritage Town, Pondicherry - 605001",
            timezone="Asia/Kolkata",
            currency="INR",
            payment_mode_config="VENUE_ONLY",
            advance_percentage=100,
            monthly_msg_quota=500,
        )
        db.session.add(clinic_tenant)
        db.session.flush()

        clinic_owner = User(
            id=uuid.uuid4(),
            tenant_id=clinic_tenant.id,
            name="Dr. S. Ramanathan",
            phone="9876543220",
            email="doctor@sriclinic.com",
            role=UserRole.OWNER,
        )
        clinic_owner.set_password("password123")

        receptionist = User(
            id=uuid.uuid4(),
            tenant_id=clinic_tenant.id,
            name="Priya Sundaram",
            phone="9876543221",
            email="reception@sriclinic.com",
            role=UserRole.RECEPTIONIST,
        )
        receptionist.set_password("password123")
        db.session.add_all([clinic_owner, receptionist])
        db.session.flush()

        doc_dental = Resource(
            id=uuid.uuid4(),
            tenant_id=clinic_tenant.id,
            name="Dr. S. Ramanathan (BDS)",
            category="Dental Consultation",
            slot_duration_mins=20,
            base_price_paise=30000,
        )
        doc_ayurveda = Resource(
            id=uuid.uuid4(),
            tenant_id=clinic_tenant.id,
            name="Dr. Lakshmi Priya (BAMS)",
            category="Ayurvedic Consultation & Panchakarma",
            slot_duration_mins=30,
            base_price_paise=40000,
        )
        db.session.add_all([doc_dental, doc_ayurveda])
        db.session.flush()

        for doc in (doc_dental, doc_ayurveda):
            for day in range(0, 6):
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=clinic_tenant.id,
                        resource_id=doc.id,
                        day_of_week=day,
                        start_time=time(9, 0),
                        end_time=time(13, 30),
                        is_break=False,
                        price_override_paise=None,
                    )
                )
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=clinic_tenant.id,
                        resource_id=doc.id,
                        day_of_week=day,
                        start_time=time(13, 30),
                        end_time=time(16, 30),
                        is_break=True,
                        price_override_paise=None,
                    )
                )
                db.session.add(
                    AvailabilityRule(
                        id=uuid.uuid4(),
                        tenant_id=clinic_tenant.id,
                        resource_id=doc.id,
                        day_of_week=day,
                        start_time=time(16, 30),
                        end_time=time(20, 30),
                        is_break=False,
                        price_override_paise=None,
                    )
                )

        db.session.commit()
        click.echo("CLINIC tenant seeded successfully.")
    else:
        click.echo("CLINIC tenant 'sri-clinic' already exists.")