import pytest
import uuid
from datetime import date, time
from app import create_app
from app.extensions import db
from app.models import (
    Tenant,
    VerticalType,
    User,
    UserRole,
    Resource,
    AvailabilityRule,
    Customer,
)


@pytest.fixture(scope="session")
def app():
    """Create application configured for testing."""
    test_app = create_app("testing")
    return test_app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()



@pytest.fixture(autouse=True)
def db_session(app):
    """Provide a clean database session per test with fresh schema."""
    with app.app_context():
        db.create_all()
        yield db.session
        db.session.remove()
        db.drop_all()


@pytest.fixture
def turf_setup(db_session):
    """Seed test turf tenant with football and badminton resources and rules."""
    tenant = Tenant(
        id=uuid.uuid4(),
        slug="test-turf",
        name="Test Pondy Arena",
        vertical=VerticalType.TURF,
        phone="9876543210",
        advance_percentage=30,
        monthly_msg_quota=500,
    )
    db_session.add(tenant)
    db_session.flush()

    football = Resource(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="5v5 Turf A",
        category="Football",
        slot_duration_mins=60,
        base_price_paise=80000,  # ₹800/hr
    )
    badminton = Resource(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="Badminton Court 1",
        category="Badminton",
        slot_duration_mins=60,
        base_price_paise=40000,  # ₹400/hr
    )
    db_session.add_all([football, badminton])
    db_session.flush()

    # Weekday rules (Mon-Fri: 0..4)
    # Off-peak: 06:00 - 17:00 (₹800 / ₹400)
    # Peak floodlight: 17:00 - 23:00 (₹1,200 / ₹600)
    # Weekend rules (Sat-Sun: 5..6)
    # All day: 06:00 - 23:00 (₹1,400 / ₹700)
    for res, base_p, peak_p, weekend_p in [
        (football, 80000, 120000, 140000),
        (badminton, 40000, 60000, 70000),
    ]:
        for day in range(0, 5):
            db_session.add(
                AvailabilityRule(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    resource_id=res.id,
                    day_of_week=day,
                    start_time=time(6, 0),
                    end_time=time(17, 0),
                    is_break=False,
                    price_override_paise=base_p,
                )
            )
            db_session.add(
                AvailabilityRule(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    resource_id=res.id,
                    day_of_week=day,
                    start_time=time(17, 0),
                    end_time=time(23, 0),
                    is_break=False,
                    price_override_paise=peak_p,
                )
            )
        for day in range(5, 7):
            db_session.add(
                AvailabilityRule(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    resource_id=res.id,
                    day_of_week=day,
                    start_time=time(6, 0),
                    end_time=time(23, 0),
                    is_break=False,
                    price_override_paise=weekend_p,
                )
            )

    customer = Customer(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="Ramesh Player",
        phone="9988776655",
        dpdp_consent_given=True,
    )
    db_session.add(customer)
    db_session.commit()

    return {
        "tenant": tenant,
        "football": football,
        "badminton": badminton,
        "customer": customer,
    }


@pytest.fixture
def clinic_setup(db_session):
    """Seed test clinic tenant with dental doctor, operating rules and afternoon break."""
    tenant = Tenant(
        id=uuid.uuid4(),
        slug="test-clinic",
        name="Test Sri Clinic",
        vertical=VerticalType.CLINIC,
        phone="9876543220",
        advance_percentage=100,
        monthly_msg_quota=500,
    )
    db_session.add(tenant)
    db_session.flush()

    doctor = Resource(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="Dr. Ramanathan (BDS)",
        category="Dental Consultation",
        slot_duration_mins=20,
        base_price_paise=30000,  # ₹300
    )
    db_session.add(doctor)
    db_session.flush()

    # Mon-Sat:
    # 09:00 - 13:30 (Morning)
    # 13:30 - 16:30 (Afternoon Break)
    # 16:30 - 20:30 (Evening)
    for day in range(0, 6):
        db_session.add(
            AvailabilityRule(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                resource_id=doctor.id,
                day_of_week=day,
                start_time=time(9, 0),
                end_time=time(13, 30),
                is_break=False,
                price_override_paise=None,
            )
        )
        db_session.add(
            AvailabilityRule(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                resource_id=doctor.id,
                day_of_week=day,
                start_time=time(13, 30),
                end_time=time(16, 30),
                is_break=True,
                price_override_paise=None,
            )
        )
        db_session.add(
            AvailabilityRule(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                resource_id=doctor.id,
                day_of_week=day,
                start_time=time(16, 30),
                end_time=time(20, 30),
                is_break=False,
                price_override_paise=None,
            )
        )

    patient = Customer(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        name="Suresh Patient",
        phone="9123456780",
        dpdp_consent_given=True,
    )
    db_session.add(patient)
    db_session.commit()

    return {
        "tenant": tenant,
        "doctor": doctor,
        "patient": patient,
    }
