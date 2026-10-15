# app/models.py
import enum
import uuid
from datetime import datetime, date, time, timezone
from typing import Optional, List, Any
from sqlalchemy import (
    String,
    Text,
    Integer,
    SmallInteger,
    Boolean,
    LargeBinary,
    DateTime,
    Date as SaDate,
    Time as SaTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    CheckConstraint,
    Enum as SaEnum,
    Uuid,
    JSON,
    func,
    text,
    types,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


class BaseModel(db.Model):
    """Abstract base model that provides Pylance/Pyright typing for keyword arguments."""
    __abstract__ = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class UTCDateTime(types.TypeDecorator):
    """Ensures datetimes are always timezone-aware in UTC across SQLite and PostgreSQL."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        return value


class VerticalType(str, enum.Enum):
    TURF = "TURF"
    CLINIC = "CLINIC"


class UserRole(str, enum.Enum):
    OWNER = "OWNER"
    DOCTOR = "DOCTOR"
    RECEPTIONIST = "RECEPTIONIST"
    STAFF = "STAFF"


class BookingStatus(str, enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    CONFIRMED = "CONFIRMED"
    IN_CONSULTATION = "IN_CONSULTATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"


class PaymentMode(str, enum.Enum):
    UPI_ONLINE = "UPI_ONLINE"
    UPI_VENUE = "UPI_VENUE"
    CASH = "CASH"
    CARD = "CARD"


class NotificationChannel(str, enum.Enum):
    WHATSAPP = "WHATSAPP"
    SMS = "SMS"


class NotificationStatus(str, enum.Enum):
    SENT = "SENT"
    FAILED = "FAILED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


class Tenant(BaseModel):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    vertical: Mapped[VerticalType] = mapped_column(
        SaEnum(VerticalType, name="vertical_type", native_enum=False),
        nullable=False
    )
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Kolkata", nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)

    # Payment Config
    payment_mode_config: Mapped[str] = mapped_column(String(30), default="VENUE_ONLY", nullable=False)
    razorpay_account_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    razorpay_key_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    razorpay_key_secret_enc: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    advance_percentage: Mapped[int] = mapped_column(Integer, default=100, nullable=False)

    # WhatsApp Quota
    monthly_msg_quota: Mapped[int] = mapped_column(Integer, default=500, nullable=False)
    monthly_msg_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False
    )

    # Relationships
    users: Mapped[List["User"]] = relationship("User", back_populates="tenant", cascade="all, delete-orphan")
    resources: Mapped[List["Resource"]] = relationship("Resource", back_populates="tenant", cascade="all, delete-orphan")
    customers: Mapped[List["Customer"]] = relationship("Customer", back_populates="tenant", cascade="all, delete-orphan")
    bookings: Mapped[List["Booking"]] = relationship("Booking", back_populates="tenant", cascade="all, delete-orphan")
    payments: Mapped[List["Payment"]] = relationship("Payment", back_populates="tenant", cascade="all, delete-orphan")
    clinic_visits: Mapped[List["ClinicVisit"]] = relationship("ClinicVisit", back_populates="tenant", cascade="all, delete-orphan")
    invoices: Mapped[List["Invoice"]] = relationship("Invoice", back_populates="tenant", cascade="all, delete-orphan")
    notification_logs: Mapped[List["NotificationLog"]] = relationship("NotificationLog", back_populates="tenant", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Tenant {self.slug} ({self.vertical.value})>"


class User(BaseModel):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone", name="uq_users_tenant_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SaEnum(UserRole, name="user_role", native_enum=False),
        nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="users")
    created_bookings: Mapped[List["Booking"]] = relationship(
        "Booking",
        back_populates="created_by_user",
        foreign_keys="[Booking.created_by_user_id]"
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self) -> str:
        return f"<User {self.name} ({self.role.value})>"


class Resource(BaseModel):
    __tablename__ = "resources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    slot_duration_mins: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    base_price_paise: Mapped[int] = mapped_column(Integer, default=50000, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="resources")
    availability_rules: Mapped[List["AvailabilityRule"]] = relationship(
        "AvailabilityRule",
        back_populates="resource",
        cascade="all, delete-orphan"
    )
    bookings: Mapped[List["Booking"]] = relationship(
        "Booking",
        back_populates="resource",
        cascade="all, delete-orphan"
    )
    clinic_visits: Mapped[List["ClinicVisit"]] = relationship(
        "ClinicVisit",
        back_populates="doctor_resource",
        foreign_keys="[ClinicVisit.doctor_resource_id]"
    )

    def __repr__(self) -> str:
        return f"<Resource {self.name}>"


class AvailabilityRule(BaseModel):
    __tablename__ = "availability_rules"
    __table_args__ = (
        CheckConstraint("day_of_week BETWEEN 0 AND 6", name="chk_rule_day_of_week"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    start_time: Mapped[time] = mapped_column(SaTime, nullable=False)
    end_time: Mapped[time] = mapped_column(SaTime, nullable=False)
    is_break: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    price_override_paise: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant")
    resource: Mapped["Resource"] = relationship("Resource", back_populates="availability_rules")

    def __repr__(self) -> str:
        return f"<AvailabilityRule Res={self.resource_id} Day={self.day_of_week} {self.start_time}-{self.end_time}>"


class Customer(BaseModel):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone", name="uq_customers_tenant_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    age: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    dpdp_consent_given: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dpdp_consent_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="customers")
    bookings: Mapped[List["Booking"]] = relationship("Booking", back_populates="customer")
    clinic_visits: Mapped[List["ClinicVisit"]] = relationship(
        "ClinicVisit",
        back_populates="customer",
        cascade="all, delete-orphan"
    )
    invoices: Mapped[List["Invoice"]] = relationship("Invoice", back_populates="customer")

    def __repr__(self) -> str:
        return f"<Customer {self.name} ({self.phone})>"


class WalkinToken(int):
    """Integer token that also satisfies string equality like 'Token #1' or '#1'."""
    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return other in (f"Token #{int(self)}", f"#{int(self)}", str(int(self)))
        return super().__eq__(other)

    def __str__(self) -> str:
        return str(int(self))

    @property
    def formatted(self) -> str:
        return f"Token #{int(self)}"


class Booking(BaseModel):
    __tablename__ = "bookings"
    __table_args__ = (
        Index(
            "idx_unique_confirmed_slot",
            "tenant_id",
            "resource_id",
            "slot_date",
            "start_time",
            unique=True,
            postgresql_where=text("status IN ('CONFIRMED', 'BLOCKED')"),
            sqlite_where=text("status IN ('CONFIRMED', 'BLOCKED')"),
        ),
        Index(
            "uq_bookings_tenant_resource_date_token",
            "tenant_id",
            "resource_id",
            "slot_date",
            "token_number",
            unique=True,
            postgresql_where=text("token_number IS NOT NULL"),
            sqlite_where=text("token_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("resources.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    slot_date: Mapped[date] = mapped_column(SaDate, nullable=False, index=True)
    start_time: Mapped[time] = mapped_column(SaTime, nullable=False)
    end_time: Mapped[time] = mapped_column(SaTime, nullable=False)
    status: Mapped[BookingStatus] = mapped_column(
        SaEnum(BookingStatus, name="booking_status", native_enum=False),
        default=BookingStatus.PENDING_PAYMENT,
        nullable=False,
        index=True
    )
    locked_until: Mapped[Optional[datetime]] = mapped_column(
        UTCDateTime,
        nullable=True,
        index=True
    )
    is_walkin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    token_number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_amount_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paid_amount_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reminder_24h_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reminder_2h_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True
    )
    booking_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="bookings")
    resource: Mapped["Resource"] = relationship("Resource", back_populates="bookings")
    customer: Mapped[Optional["Customer"]] = relationship("Customer", back_populates="bookings")
    created_by_user: Mapped[Optional["User"]] = relationship(
        "User",
        back_populates="created_bookings",
        foreign_keys=[created_by_user_id]
    )
    payments: Mapped[List["Payment"]] = relationship(
        "Payment",
        back_populates="booking",
        cascade="all, delete-orphan"
    )
    clinic_visit: Mapped[Optional["ClinicVisit"]] = relationship(
        "ClinicVisit",
        back_populates="booking",
        uselist=False
    )
    invoices: Mapped[List["Invoice"]] = relationship("Invoice", back_populates="booking")

    @classmethod
    def generate_next_walkin_token(
        cls,
        tenant_id: uuid.UUID | str,
        doctor_resource_id: uuid.UUID | str,
        slot_date: date,
    ) -> WalkinToken:
        """Locks the doctor Resource row and computes the next sequential walk-in token."""
        # 1. Acquire row-level lock on the doctor Resource row
        db.session.execute(
            select(Resource)
            .filter_by(id=doctor_resource_id, tenant_id=tenant_id)
            .with_for_update()
        ).scalar_one()

        # 2. Safely count/max existing walk-in tokens for this doctor and date
        existing_tokens = (
            db.session.execute(
                select(cls.token_number).filter(
                    cls.tenant_id == tenant_id,
                    cls.resource_id == doctor_resource_id,
                    cls.slot_date == slot_date,
                    cls.token_number.is_not(None),
                )
            )
            .scalars()
            .all()
        )

        max_num = 0
        for tok in existing_tokens:
            if isinstance(tok, int):
                max_num = max(max_num, tok)
            elif tok is not None:
                digits = "".join(ch for ch in str(tok) if ch.isdigit())
                if digits:
                    max_num = max(max_num, int(digits))

        return WalkinToken(max_num + 1)

    def __repr__(self) -> str:
        return f"<Booking {self.id} Res={self.resource_id} {self.slot_date} {self.start_time} ({self.status.value})>"


class Payment(BaseModel):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    booking_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bookings.id", ondelete="CASCADE"),
        nullable=True,
        index=True
    )
    invoice_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True)
    razorpay_order_id: Mapped[Optional[str]] = mapped_column(String(100), unique=True, nullable=True)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String(100), unique=True, nullable=True)
    amount_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    mode: Mapped[PaymentMode] = mapped_column(
        SaEnum(PaymentMode, name="payment_mode", native_enum=False),
        nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="CREATED", nullable=False)
    webhook_payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="payments")
    booking: Mapped[Optional["Booking"]] = relationship("Booking", back_populates="payments")

    def __repr__(self) -> str:
        return f"<Payment {self.id} Amount={self.amount_paise} Mode={self.mode.value} Status={self.status}>"


class ClinicVisit(BaseModel):
    __tablename__ = "clinic_visits"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    booking_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    doctor_resource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("resources.id"),
        nullable=False,
        index=True
    )
    chief_complaint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    clinical_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    procedures_done: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_followup_date: Mapped[Optional[date]] = mapped_column(SaDate, nullable=True)
    followup_reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="clinic_visits")
    booking: Mapped[Optional["Booking"]] = relationship("Booking", back_populates="clinic_visit")
    customer: Mapped["Customer"] = relationship("Customer", back_populates="clinic_visits")
    doctor_resource: Mapped["Resource"] = relationship(
        "Resource",
        back_populates="clinic_visits",
        foreign_keys=[doctor_resource_id]
    )

    def __repr__(self) -> str:
        return f"<ClinicVisit {self.id} Patient={self.customer_id} Doctor={self.doctor_resource_id}>"


class Invoice(BaseModel):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "invoice_number", name="uq_invoices_tenant_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id"),
        nullable=False,
        index=True
    )
    booking_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bookings.id"),
        nullable=True
    )
    invoice_number: Mapped[str] = mapped_column(String(50), nullable=False)
    line_items: Mapped[Any] = mapped_column(JSON, nullable=False)
    total_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    balance_paise: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    payment_mode: Mapped[Optional[PaymentMode]] = mapped_column(
        SaEnum(PaymentMode, name="payment_mode", native_enum=False),
        nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="invoices")
    customer: Mapped["Customer"] = relationship("Customer", back_populates="invoices")
    booking: Mapped[Optional["Booking"]] = relationship("Booking", back_populates="invoices")

    @classmethod
    def generate_next_invoice_number(
        cls,
        tenant_id: uuid.UUID | str,
        prefix: str = "INV-SRI",
    ) -> str:
        """Locks the Tenant row before computing the next sequential invoice number."""
        # 1. Acquire row-level lock on the Tenant row
        db.session.execute(
            select(Tenant)
            .filter_by(id=tenant_id)
            .with_for_update()
        ).scalar_one()

        # 2. Safely compute the next sequential invoice number for this tenant
        existing_numbers = (
            db.session.execute(
                select(cls.invoice_number).filter_by(tenant_id=tenant_id)
            )
            .scalars()
            .all()
        )

        max_seq = 0
        for inv_num in existing_numbers:
            parts = str(inv_num).rsplit("-", 1)
            if len(parts) >= 2 and parts[-1].isdigit():
                max_seq = max(max_seq, int(parts[-1]))

        month_str = date.today().strftime('%y%m')
        return f"{prefix}-{month_str}-{max_seq + 1:04d}"

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_number} Total={self.total_paise}>"


class AuditLog(BaseModel):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} Tenant={self.tenant_id}>"


class NotificationLog(BaseModel):
    __tablename__ = "notification_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    booking_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("bookings.id", ondelete="SET NULL"),
        nullable=True
    )
    customer_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("customers.id", ondelete="SET NULL"),
        nullable=True
    )
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        SaEnum(NotificationChannel, name="notification_channel", native_enum=False),
        nullable=False
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    template_name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        SaEnum(NotificationStatus, name="notification_status", native_enum=False),
        default=NotificationStatus.SENT,
        nullable=False
    )
    payload: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        server_default=func.now(),
        nullable=False
    )

    tenant: Mapped["Tenant"] = relationship("Tenant", back_populates="notification_logs")
    booking: Mapped[Optional["Booking"]] = relationship("Booking")
    customer: Mapped[Optional["Customer"]] = relationship("Customer")

    def __repr__(self) -> str:
        return f"<NotificationLog {self.channel.value}:{self.template_name} to={self.recipient_phone} ({self.status.value})>"