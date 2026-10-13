# Technical Requirements Document (TRD): SlotEngine

## 1. System Architecture & Directory Structure

```text
slotengine/
├── app/
│   ├── __init__.py          # Flask app factory, extensions init
│   ├── config.py            # Dev, Test, Prod configs
│   ├── models.py            # SQLAlchemy 2.0 models & indexes
│   ├── extensions.py        # db, migrate, scheduler, csrf
│   ├── middleware/
│   │   ├── tenant.py        # Resolves tenant from slug/session
│   │   └── auth.py          # RBAC decorators & audit logging
│   ├── services/
│   │   ├── slot_engine.py   # Availability calculation & atomic holds
│   │   ├── pricing.py       # Peak/off-peak & weekday/weekend calculator
│   │   ├── payment_gw.py    # Razorpay orders, Route splits, webhook HMAC
│   │   ├── notifier.py      # WhatsApp Cloud API / AiSensy + SMS fallback
│   │   └── pdf_invoice.py   # WeasyPrint / ReportLab PDF generator
│   ├── blueprints/
│   │   ├── public/          # /b/<slug> customer booking & HTMX partials
│   │   ├── dashboard/       # Owner/Staff/Receptionist unified dashboard
│   │   ├── clinic/          # Patients, tokens, visits, invoices routes
│   │   ├── webhooks/        # /webhooks/razorpay, /webhooks/whatsapp
│   │   └── auth/            # Login, logout, staff invite
│   └── templates/
│       ├── base.html
│       ├── public/          # Mobile-first booking templates + partials
│       ├── dashboard/       # Turf & Clinic dashboard views
│       └── clinic/          # Queue board, patient record, invoice PDF
├── migrations/
├── tests/                   # pytest suite (concurrency, pricing, webhooks)
├── wsgi.py
└── requirements.txt

## 2. Complete PostgreSQL Database Schema
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TYPE vertical_type AS ENUM ('TURF', 'CLINIC');
CREATE TYPE user_role AS ENUM ('OWNER', 'DOCTOR', 'RECEPTIONIST', 'STAFF');
CREATE TYPE booking_status AS ENUM ('PENDING_PAYMENT', 'CONFIRMED', 'COMPLETED', 'CANCELLED', 'NO_SHOW', 'BLOCKED', 'EXPIRED');
CREATE TYPE payment_mode AS ENUM ('UPI_ONLINE', 'UPI_VENUE', 'CASH', 'CARD');

-- 1. Tenants (Businesses)
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug VARCHAR(64) UNIQUE NOT NULL,
    name VARCHAR(150) NOT NULL,
    vertical vertical_type NOT NULL,
    phone VARCHAR(20) NOT NULL,
    address TEXT,
    timezone VARCHAR(50) DEFAULT 'Asia/Kolkata' NOT NULL,
    currency VARCHAR(10) DEFAULT 'INR' NOT NULL,
    -- Payment Config
    payment_mode_config VARCHAR(30) DEFAULT 'VENUE_ONLY', -- 'RAZORPAY_ROUTE', 'RAZORPAY_OWN', 'VENUE_ONLY'
    razorpay_account_id VARCHAR(100),
    razorpay_key_id VARCHAR(100),
    razorpay_key_secret_enc BYTEA,
    advance_percentage INT DEFAULT 100, -- e.g. 30% advance or 100% full
    -- WhatsApp Quota
    monthly_msg_quota INT DEFAULT 500,
    monthly_msg_used INT DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. Users (Owners, Doctors, Staff, Receptionists)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    email VARCHAR(120),
    password_hash VARCHAR(255) NOT NULL,
    role user_role NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, phone)
);

-- 3. Resources (Courts/Turfs OR Doctors/Rooms)
CREATE TABLE resources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL, -- e.g. "5v5 AstroTurf" or "Dr. Aravind (BDS)"
    category VARCHAR(80),       -- e.g. "Football/Cricket" or "Dental Consultation"
    slot_duration_mins INT NOT NULL DEFAULT 60, -- 60/30 for Turf, 15/20/30 for Clinic
    base_price_paise INT NOT NULL DEFAULT 50000, -- ₹500.00 stored in paise
    is_active BOOLEAN DEFAULT TRUE
);

-- 4. Availability & Dynamic Pricing Rules
CREATE TABLE availability_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    resource_id UUID NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6), -- 0=Monday, 6=Sunday
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    is_break BOOLEAN DEFAULT FALSE, -- True for afternoon clinic break
    price_override_paise INT        -- e.g. Peak evening or weekend price
);

-- 5. Customers / Patients (Unified Contact + DPDP Consent)
CREATE TABLE customers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name VARCHAR(120) NOT NULL,
    phone VARCHAR(20) NOT NULL,
    age SMALLINT,
    gender VARCHAR(15),
    dpdp_consent_given BOOLEAN DEFAULT FALSE,
    dpdp_consent_at TIMESTAMPTZ,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, phone)
);

-- 6. Bookings & Walk-in Tokens
CREATE TABLE bookings (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    resource_id UUID NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    customer_id UUID REFERENCES customers(id) ON DELETE SET NULL,
    slot_date DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    status booking_status NOT NULL DEFAULT 'PENDING_PAYMENT',
    locked_until TIMESTAMPTZ, -- For 7-minute payment holds
    is_walkin BOOLEAN DEFAULT FALSE,
    token_number INT,         -- Sequential daily token for CLINIC
    total_amount_paise INT NOT NULL DEFAULT 0,
    paid_amount_paise INT NOT NULL DEFAULT 0,
    created_by_user_id UUID REFERENCES users(id), -- Null if self-booked online
    booking_notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- CRITICAL INDEX: Prevents double-booking active confirmed/blocked slots
CREATE UNIQUE INDEX idx_unique_confirmed_slot 
ON bookings (tenant_id, resource_id, slot_date, start_time) 
WHERE status IN ('CONFIRMED', 'BLOCKED');

-- 7. Payments
CREATE TABLE payments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    booking_id UUID REFERENCES bookings(id) ON DELETE CASCADE,
    invoice_id UUID,
    razorpay_order_id VARCHAR(100) UNIQUE,
    razorpay_payment_id VARCHAR(100) UNIQUE,
    amount_paise INT NOT NULL,
    mode payment_mode NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'CREATED', -- CREATED, CAPTURED, FAILED, REFUNDED
    webhook_payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 8. Clinic Visits & Follow-ups (CLINIC Vertical Only)
CREATE TABLE clinic_visits (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    booking_id UUID REFERENCES bookings(id) ON DELETE SET NULL,
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    doctor_resource_id UUID NOT NULL REFERENCES resources(id),
    chief_complaint TEXT,
    clinical_notes TEXT, -- Restricted to OWNER/DOCTOR roles
    procedures_done TEXT,
    next_followup_date DATE,
    followup_reminder_sent BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 9. Invoices (CLINIC & Optional TURF Receipts)
CREATE TABLE invoices (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id UUID NOT NULL REFERENCES customers(id),
    booking_id UUID REFERENCES bookings(id),
    invoice_number VARCHAR(50) NOT NULL,
    line_items JSONB NOT NULL, -- [{"desc": "Consultation", "amount_paise": 30000}]
    total_paise INT NOT NULL,
    paid_paise INT NOT NULL DEFAULT 0,
    balance_paise INT NOT NULL DEFAULT 0,
    payment_mode payment_mode,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, invoice_number)
);

-- 10. Audit & Notification Logs (DPDP Compliance + Quota Tracking)
CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,
    user_id UUID,
    action VARCHAR(100) NOT NULL, -- e.g. 'VIEW_PATIENT_RECORD', 'EXPORT_DATA'
    target_id UUID,
    ip_address VARCHAR(45),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
