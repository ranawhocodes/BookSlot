# SlotEngine (BookSlot)

> **Multi-Tenant Booking, Queue, and Billing Engine for Indian SMBs — "One Engine, Two Skins"**

SlotEngine is a lightweight, mobile-first multi-tenant SaaS built with Python Flask 3.x, SQLAlchemy 2.0, PostgreSQL/SQLite, Tailwind CSS, and HTMX.

---

## 🎯 The Verticals

1. **Skin A (`TURF`):** Sports turfs, box cricket grounds, badminton courts, pickleball clubs.
   - Solves double-bookings, phone call chaos, and advance UPI collection.
   - Features dynamic weekday/weekend & floodlight peak pricing, 7-minute atomic holds, owner manual booking modal, and printable venue QR code posters.
2. **Skin B (`CLINIC`):** Small medical clinics, dental practices, physiotherapy, and Ayurveda centres.
   - Solves chaotic waiting rooms, walk-in token queues, paper registers, and manual billing.
   - Features hybrid appointment + walk-in token queue board, EMR-lite patient profiles, itemized PDF receipt generator, and DPDP Act 2023 compliance.

---

## 🚀 Key Features

- **Zero Double-Bookings:** Atomic check-and-insert holds (`SELECT ... FOR UPDATE` & unique index constraints) with automatic 7-minute hold expiration.
- **Mobile-First Public Booking (`/b/<slug>`):** 95% of customers book via WhatsApp in-app browser. Optimized for under 100KB payloads and 3-tap checkout.
- **English / Tamil UI Localization:** Native bilingual dictionary support (`/b/<slug>?lang=ta`) with localized date navigation and booking terms.
- **Notification Engine:** Adapter pattern supporting Meta WhatsApp Cloud API, AiSensy BSP, and automatic SMS fallback with monthly quota limits.
- **Automated Reminders CLI (`flask send-reminders`):** Scans 24h & 2h upcoming bookings and clinic follow-ups with database-level idempotency.
- **RBAC Matrix:** Strict role enforcement (`OWNER`, `DOCTOR`, `RECEPTIONIST`, `STAFF`) with clinic audit logging.

---

## 🛠️ Tech Stack

- **Backend:** Python 3.12+, Flask 3.x (Application Factory Pattern with Blueprints)
- **Database & ORM:** SQLAlchemy 2.0, Alembic / Flask-Migrate, PostgreSQL / SQLite
- **Frontend:** Server-Side Rendered Jinja2 + HTMX 2.x + Tailwind CSS (No React/Vue required)
- **Testing:** Pytest (100% green coverage on pricing, concurrency, EMR, and notifications)

---

## 🏁 Quickstart

### 1. Setup Virtual Environment & Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # Or on Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` (or configure via environment):
```ini
FLASK_ENV=development
SECRET_KEY=your-secret-key
APP_MASTER_KEY=c2xvdGVuZ2luZS1tYXN0ZXIta2V5LTMyYnl0ZXM=
DATABASE_URL=sqlite:///instance/slotengine_dev.db
TIMEZONE=Asia/Kolkata
```

### 3. Seed Demo Tenants
```bash
flask seed-demo
```
This creates:
- **Turf Demo:** `Pondy Arena Turf` (`/b/pondy-arena`)
- **Clinic Demo:** `Sri Aurobindo Dental & Ayurveda` (`/dashboard/clinic/queue?slug=sri-clinic`)

### 4. Run Development Server
```bash
python wsgi.py
# Server runs on http://127.0.0.1:5000
```

### 5. Run Automated Reminders
```bash
flask send-reminders
```

### 6. Run Test Suite
```bash
pytest -v
```

---

## 📂 Project Architecture

```text
slotengine/
├── app/
│   ├── __init__.py          # Flask app factory, blueprint registration
│   ├── config.py            # Development, testing, production configurations
│   ├── models.py            # SQLAlchemy 2.0 models & unique indexes
│   ├── extensions.py        # db, migrate, csrf
│   ├── translations.py      # English & Tamil localization dictionary
│   ├── cli.py               # CLI commands (seed-demo, send-reminders)
│   ├── middleware/
│   │   ├── tenant.py        # Multi-tenant slug & session resolver
│   │   └── auth.py          # RBAC decorators & audit logging
│   ├── services/
│   │   ├── slot_engine.py       # Slot availability & atomic 7-minute holds
│   │   ├── pricing.py           # Peak/off-peak & advance price calculator
│   │   ├── payment_gw.py        # UPI deep links, QR codes, Razorpay & Fernet encryption
│   │   ├── notifier.py          # Meta WhatsApp Cloud API / AiSensy + SMS fallback
│   │   └── background_runner.py # Threaded scheduler for automated reminders & hold cleanup
│   ├── utils/
│   │   └── validators.py        # Strict Indian phone regex, honeypot & IP rate limiter
│   ├── blueprints/
│   │   ├── public/              # /b/<slug> customer booking & HTMX partials
│   │   ├── dashboard/           # Owner turf/clinic dashboard & 60s onboarding wizard
│   │   ├── clinic/              # Queue board, EMR patient records, invoices
│   │   └── webhooks/            # Razorpay & WhatsApp webhooks
│   └── templates/               # Jinja2 templates for public, turf, and clinic
├── docs/                        # PRD, TRD, and Client Onboarding Roadmap (.docx)
├── scripts/                     # Word document & asset generators
├── tests/                       # 38 pytest test cases (100% green)
├── render.yaml                  # Production PaaS blueprint for Render + PostgreSQL
└── requirements.txt
```
