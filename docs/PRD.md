# Product Requirements Document (PRD): SlotEngine ("One Engine, Two Skins")

## 1. Product Vision & Strategy

SlotEngine is a lightweight, mobile-first multi-tenant booking and operations platform tailored for Indian SMBs in two verticals:

- **Skin A (`TURF` - Phase 1 Launch):** Turfs, box cricket, badminton courts, and pickleball clubs. Solves double-bookings, phone-call coordination, and advance UPI collection.
- **Skin B (`CLINIC` - Phase 2 Launch):** Small clinics, dental practices, physiotherapy, and Ayurveda centres. Solves patient no-shows, chaotic waiting room walk-in queues, paper registers, and manual billing.

## 2. User Personas & RBAC Matrix

| Role | Vertical | Capabilities | Restrictions |
| :--- | :--- | :--- | :--- |
| **Customer / Patient** | Both | Browse `/b/<slug>`, pick resource & date, book slot, pay UPI advance, receive WhatsApp alerts. | No login required (Phone + Name + OTP/WhatsApp). |
| **Owner / Admin** | Both | Full access: configure resources, pricing rules, block slots, manual bookings, revenue dashboard, data export, staff management. | None within their `tenant_id`. |
| **Staff / Manager** | `TURF` | Create manual walk-in/phone bookings, mark balance collected at venue, block maintenance slots, view today's schedule. | Cannot edit gateway settings or delete historical revenue records. |
| **Doctor** | `CLINIC` | View personal appointment & token queue, view/update patient visit history & follow-up date, trigger follow-up reminder. | Scoped to clinic patients. |
| **Receptionist** | `CLINIC` | Manage all doctor schedules, issue walk-in tokens, check in patients, generate PDF invoices, record payments. | Cannot view sensitive clinical notes if restricted by Admin. |

## 3. Functional Requirements: Shared Core Engine

1. **Tenant Onboarding & Public Page (`/b/<slug>`):**
   - Custom slug (e.g., `brand.in/b/strikersturf` or `brand.in/b/keralaayurveda`), downloadable high-contrast printable QR Code poster (PDF/PNG), and language toggle (English / Tamil UI labels).
2. **Resource & Availability Engine:**
   - Resources represent Courts/Turfs (`TURF`) or Doctors/Therapy Rooms (`CLINIC`).
   - Configurable slot duration (15, 20, 30, 60, or 90 mins), working hours per day of week, break windows (e.g., 1:30 PM – 4:00 PM clinic break), and holiday/maintenance blockouts.
3. **Notification Engine (WhatsApp First + SMS Fallback):**
   - Pluggable provider interface supporting official Meta Cloud API / AiSensy BSP.
   - Automatic fallback to SMS if WhatsApp template delivery fails or is unconfigured.
   - Monthly tenant message counter (`messages_used_this_month` vs `monthly_message_quota`) to protect SaaS unit economics.

## 4. Functional Requirements: Skin A — Turf & Court Module (`TURF`)

1. **Dynamic Slot Pricing Engine:**
   - Base price per slot with rule overrides based on:
     - **Day Type:** Weekday (Mon–Fri) vs Weekend (Sat–Sun).
     - **Time Band:** Off-Peak (e.g., 6 AM – 4 PM: ₹600/hr) vs Peak Floodlight Hours (e.g., 5 PM – 11 PM: ₹1,000/hr).
2. **Live Customer Booking & Hold Flow:**
   - Customer selects Resource (e.g., "5v5 Turf A" or "Synthetic Court 2"), Date, and one or more contiguous slots.
   - Customer opts to pay **Advance Amount** (e.g., ₹200 fixed or 30%) or **Full Amount** via UPI.
   - Creating checkout places a **7-minute atomic lock** on the selected slots.
3. **Owner Manual Booking & Walk-In Override (Critical Adoption Feature):**
   - 2-tap modal on the owner's mobile dashboard to book a slot for a phone caller ("Ramesh - 9876543210 - Cash/UPI Direct - ₹200 Advance Paid, ₹800 Pending at Venue").
4. **Recurring Bookings & Maintenance Blocks:**
   - Ability to book a recurring weekly slot (e.g., "Every Sunday 7 AM – 9 AM for 4 weeks") or block slots for tournaments/maintenance.
5. **Turf Revenue & Occupancy Dashboard:**
   - Today's total bookings, cash vs online UPI collected, pending venue balance to collect, and slot occupancy percentage.

## 5. Functional Requirements: Skin B — Clinic & Ayurveda Module (`CLINIC`)

1. **Appointment + Walk-In Token Hybrid Queue:**
   - Combines pre-booked time slots with a live **Walk-In Token Generator** (Token #1, #2, #3...).
   - Live Receptionist Queue Board showing `WAITING`, `IN_CONSULTATION`, `COMPLETED`, and `NO_SHOW`.
2. **Patient Profile & Visit History (EMR-Lite):**
   - Searchable by phone number or name.
   - Stores basic demographics, DPDP consent timestamp, visit log, chief complaint/procedure notes, and `next_followup_date`.
   - *Strict Scope Boundary:* No drug prescription builders or diagnostic AI in v1 to avoid clinical liability.
3. **Automated Reminders & Follow-Up Loop:**
   - Utility WhatsApp reminders sent **24 hours** and **2 hours** prior to appointment.
   - Automated morning WhatsApp reminder sent 2 days before `next_followup_date` ("Dr. Kumar requested a follow-up check on [Date]. Tap here to pick a slot").
4. **Simple Invoice Generator & Pending Balance Tracker:**
   - Itemized bill builder (Consultation Fee, Dental Scaling, Panchakarma Session, etc.), payment mode (`UPI`, `CASH`, `CARD`), amount paid, and `balance_due`.
   - Generates a clean, printable/shareable A4/A5 PDF receipt and WhatsApp link.
5. **One-Click CSV/JSON Data Export & DPDP Compliance:**
   - Explicit patient consent checkbox on booking form with timestamp audit log.
   - Full CSV export of patients, visits, and invoices so the doctor never feels locked in.
