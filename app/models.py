import enum
import uuid
from datetime import datetime, date, time
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
)
from datetime import timezone
from sqlalchemy.orm import Mapped, mapped_column, relationship
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


class UTCDateTime(types.TypeDecorator):
    """
    Ensures datetimes are always timezone-aware in UTC across SQLite and PostgreSQL.
    """
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


class Tenant(db.Model):
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
        default=lambda: datetime.now(datetime.timezone.utc if hasattr(datetime, "timezone") else None),
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


class User(db.Model):
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


class Resource(db.Model):
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


class AvailabilityRule(db.Model):
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
    day_of_week: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0=Monday, 6=Sunday
    start_time: Mapped[time] = mapped_column(SaTime, nullable=False)
    end_time: Mapped[time] = mapped_column(SaTime, nullable=False)
    is_break: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    price_override_paise: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    tenant: Mapped["Tenant"] = relationship("Tenant")
    resource: Mapped["Resource"] = relationship("Resource", back_populates="availability_rules")

    def __repr__(self) -> str:
        return f"<AvailabilityRule Res={self.resource_id} Day={self.day_of_week} {self.start_time}-{self.end_time}>"


class Customer(db.Model):
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


class Booking(db.Model):
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

    def __repr__(self) -> str:
        return f"<Booking {self.id} Res={self.resource_id} {self.slot_date} {self.start_time} ({self.status.value})>"


class Payment(db.Model):
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


class ClinicVisit(db.Model):
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


class Invoice(db.Model):
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
    line_items: Mapped[Any] = mapped_column(JSON, nullable=False)  # e.g. [{"desc": "...", "amount_paise": 30000}]
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

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_number} Total={self.total_paise}>"


class AuditLog(db.Model):
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


class NotificationLog(db.Model):
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
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # META, AISENSY, SMS_FALLBACK
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

