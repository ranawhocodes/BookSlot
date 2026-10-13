import click
import uuid
from datetime import time
from flask import Flask
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


def seed_demo_data(reset: bool = False) -> None:
    """Idempotently seeds demo tenants, resources, pricing rules, and users."""
    # Ensure tables exist
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
            advance_percentage=30,  # 30% advance via UPI
            monthly_msg_quota=500,
        )
        db.session.add(turf_tenant)
        db.session.flush()

        # Turf Owner User
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

        # Resources: 5v5 Turf + 2 Badminton Courts
        turf_football = Resource(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="5v5 Football Turf",
            category="Football",
            slot_duration_mins=60,
            base_price_paise=80000,  # ₹800/hr off-peak
        )
        badminton_1 = Resource(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="Badminton Court 1",
            category="Badminton",
            slot_duration_mins=60,
            base_price_paise=40000,  # ₹400/hr off-peak
        )
        badminton_2 = Resource(
            id=uuid.uuid4(),
            tenant_id=turf_tenant.id,
            name="Badminton Court 2",
            category="Badminton",
            slot_duration_mins=60,
            base_price_paise=40000,  # ₹400/hr off-peak
        )
        db.session.add_all([turf_football, badminton_1, badminton_2])
        db.session.flush()

        # Pricing rules for Football Turf:
        # Weekdays (0-4): 06:00-17:00 Base (₹800), 17:00-23:00 Peak Floodlight (₹1,200)
        # Weekends (5-6): 06:00-23:00 Weekend Peak (₹1,400)
        turf_resources = [
            (turf_football, 80000, 120000, 140000),
            (badminton_1, 40000, 60000, 70000),
            (badminton_2, 40000, 60000, 70000),
        ]

        for res, base_p, peak_p, weekend_p in turf_resources:
            # Weekdays (Mon-Fri: 0..4)
            for day in range(0, 5):
                # Off-peak rule
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
                # Peak Floodlight rule
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
            # Weekends (Sat-Sun: 5..6)
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

        # Clinic Users: Doctor Owner + Receptionist
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

        # Resources: 2 Doctors
        doc_dental = Resource(
            id=uuid.uuid4(),
            tenant_id=clinic_tenant.id,
            name="Dr. S. Ramanathan (BDS)",
            category="Dental Consultation",
            slot_duration_mins=20,  # 20-min dental consultation slots
            base_price_paise=30000,  # ₹300 consultation fee
        )
        doc_ayurveda = Resource(
            id=uuid.uuid4(),
            tenant_id=clinic_tenant.id,
            name="Dr. Lakshmi Priya (BAMS)",
            category="Ayurvedic Consultation & Panchakarma",
            slot_duration_mins=30,  # 30-min ayurveda consultation slots
            base_price_paise=40000,  # ₹400 consultation fee
        )
        db.session.add_all([doc_dental, doc_ayurveda])
        db.session.flush()

        # Operating schedule: Mon - Sat (0..5), Sunday closed
        # Morning: 09:00 - 13:30 (operating)
        # Break: 13:30 - 16:30 (is_break=True)
        # Evening: 16:30 - 20:30 (operating)
        for doc in (doc_dental, doc_ayurveda):
            for day in range(0, 6):
                # Morning session
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
                # Afternoon break
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
                # Evening session
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
