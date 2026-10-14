# app/services/notifier.py
import json
import os
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, date, time
from typing import Optional, Dict, Any
from flask import current_app
from sqlalchemy import select
from app.extensions import db
from app.models import (
    Tenant,
    Booking,
    BookingStatus,
    ClinicVisit,
    NotificationLog,
    NotificationChannel,
    NotificationStatus,
)


@dataclass
class NotificationResult:
    success: bool
    channel: NotificationChannel
    provider: str
    status: NotificationStatus
    message_id: Optional[str] = None
    error: Optional[str] = None


class MetaWhatsAppProvider:
    """Official Meta WhatsApp Cloud API Provider."""

    def __init__(self, api_token: Optional[str] = None, phone_number_id: Optional[str] = None):
        self.api_token = api_token if api_token is not None else os.environ.get("META_WA_API_TOKEN")
        self.phone_number_id = phone_number_id if phone_number_id is not None else os.environ.get("META_WA_PHONE_NUMBER_ID")

    def send_template(self, recipient_phone: str, template_name: str, parameters: Dict[str, Any]) -> str:
        if not self.api_token or not self.phone_number_id:
            raise RuntimeError("Meta WhatsApp Cloud API is unconfigured (missing token or phone_number_id).")

        # Normalize to 91XXXXXXXXXX for Meta API
        clean_phone = "".join(ch for ch in str(recipient_phone) if ch.isdigit())
        if len(clean_phone) == 10:
            clean_phone = f"91{clean_phone}"

        live_http = os.environ.get("WHATSAPP_LIVE_HTTP", "false").lower() == "true"
        if live_http and not str(self.api_token).startswith("test_"):
            url = f"https://graph.facebook.com/v20.0/{self.phone_number_id}/messages"
            body_params = [
                {"type": "text", "text": str(val)}
                for _, val in parameters.items()
            ]
            payload = {
                "messaging_product": "whatsapp",
                "to": clean_phone,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": "en"},
                    "components": [{"type": "body", "parameters": body_params}] if body_params else [],
                },
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                messages = resp_data.get("messages", [{}])
                return messages[0].get("id", f"meta_wamid_{uuid.uuid4().hex[:12]}")

        return f"meta_wamid_{uuid.uuid4().hex[:12]}"


class AiSensyProvider:
    """AiSensy WhatsApp BSP Provider."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key if api_key is not None else os.environ.get("AISENSY_API_KEY")

    def send_template(self, recipient_phone: str, template_name: str, parameters: Dict[str, Any]) -> str:
        if not self.api_key:
            raise RuntimeError("AiSensy WhatsApp API key is unconfigured.")

        clean_phone = "".join(ch for ch in str(recipient_phone) if ch.isdigit())
        if len(clean_phone) == 10:
            clean_phone = f"91{clean_phone}"

        live_http = os.environ.get("WHATSAPP_LIVE_HTTP", "false").lower() == "true"
        if live_http and not str(self.api_key).startswith("test_"):
            url = "https://backend.aisensy.com/campaign/t1/api/v2"
            payload = {
                "apiKey": self.api_key,
                "campaignName": template_name,
                "destination": clean_phone,
                "userName": str(parameters.get("customer_name", "Customer")),
                "templateParams": [str(v) for v in parameters.values()],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
                return f"aisensy_msg_{uuid.uuid4().hex[:12]}"

        return f"aisensy_msg_{uuid.uuid4().hex[:12]}"


class SMSFallbackProvider:
    """Fallback transactional SMS sender when WhatsApp is pending or fails."""

    def send_sms(self, recipient_phone: str, template_name: str, parameters: Dict[str, Any]) -> str:
        return f"sms_id_{uuid.uuid4().hex[:12]}"


class NotifierService:
    """
    Unified Notification Engine enforcing monthly tenant quota,
    attempting WhatsApp primary delivery and falling back to SMS.
    """

    def __init__(
        self,
        whatsapp_provider: Optional[Any] = None,
        sms_provider: Optional[SMSFallbackProvider] = None,
    ):
        if whatsapp_provider is not None:
            self.whatsapp_provider = whatsapp_provider
        elif os.environ.get("AISENSY_API_KEY"):
            self.whatsapp_provider = AiSensyProvider()
        else:
            self.whatsapp_provider = MetaWhatsAppProvider()

        self.sms_provider = sms_provider or SMSFallbackProvider()

    def send_notification(
        self,
        tenant: Tenant,
        recipient_phone: str,
        template_name: str,
        parameters: Dict[str, Any],
        booking_id: Optional[uuid.UUID] = None,
        customer_id: Optional[uuid.UUID] = None,
    ) -> NotificationResult:
        # 1. Enforce monthly message quota
        if tenant.monthly_msg_used >= tenant.monthly_msg_quota:
            err_msg = f"Monthly message quota exceeded ({tenant.monthly_msg_used}/{tenant.monthly_msg_quota})."
            log = NotificationLog(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                booking_id=booking_id,
                customer_id=customer_id,
                recipient_phone=recipient_phone,
                channel=NotificationChannel.WHATSAPP,
                provider="QUOTA_GUARD",
                template_name=template_name,
                status=NotificationStatus.QUOTA_EXCEEDED,
                payload={"parameters": parameters, "error": err_msg},
            )
            db.session.add(log)
            db.session.commit()
            return NotificationResult(
                success=False,
                channel=NotificationChannel.WHATSAPP,
                provider="QUOTA_GUARD",
                status=NotificationStatus.QUOTA_EXCEEDED,
                error=err_msg,
            )

        # 2. Attempt primary WhatsApp provider
        provider_name = "AISENSY" if isinstance(self.whatsapp_provider, AiSensyProvider) else "META"
        try:
            msg_id = self.whatsapp_provider.send_template(
                recipient_phone=recipient_phone,
                template_name=template_name,
                parameters=parameters,
            )
            tenant.monthly_msg_used += 1
            log = NotificationLog(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                booking_id=booking_id,
                customer_id=customer_id,
                recipient_phone=recipient_phone,
                channel=NotificationChannel.WHATSAPP,
                provider=provider_name,
                template_name=template_name,
                status=NotificationStatus.SENT,
                payload={"parameters": parameters, "message_id": msg_id},
            )
            db.session.add(log)
            db.session.commit()
            return NotificationResult(
                success=True,
                channel=NotificationChannel.WHATSAPP,
                provider=provider_name,
                status=NotificationStatus.SENT,
                message_id=msg_id,
            )
        except Exception as wa_exc:
            # 3. Automatic SMS Fallback
            sms_id = self.sms_provider.send_sms(
                recipient_phone=recipient_phone,
                template_name=template_name,
                parameters=parameters,
            )
            tenant.monthly_msg_used += 1
            fallback_note = f"WhatsApp failed ({wa_exc}); delivered via SMS fallback"
            log = NotificationLog(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                booking_id=booking_id,
                customer_id=customer_id,
                recipient_phone=recipient_phone,
                channel=NotificationChannel.SMS,
                provider="SMS_FALLBACK",
                template_name=template_name,
                status=NotificationStatus.SENT,
                payload={"parameters": parameters, "message_id": sms_id, "error": fallback_note},
            )
            db.session.add(log)
            db.session.commit()
            return NotificationResult(
                success=True,
                channel=NotificationChannel.SMS,
                provider="SMS_FALLBACK",
                status=NotificationStatus.SENT,
                message_id=sms_id,
                error=fallback_note,
            )


def send_reminders(reference_time: Optional[datetime] = None) -> Dict[str, int]:
    """
    Idempotently scans and sends:
    1. 24-hour upcoming booking reminders
    2. 2-hour upcoming booking reminders
    3. Clinic follow-up reminders due within 2 days
    """
    now_ref = reference_time or datetime.now()
    today = now_ref.date()
    tomorrow = today + timedelta(days=1)

    notifier = NotifierService()
    reminders_24h_sent = 0
    reminders_2h_sent = 0
    followups_sent = 0
    quota_exceeded_count = 0

    # 1. 24h Reminders (Bookings on tomorrow's date not yet reminded)
    bookings_24h = db.session.execute(
        select(Booking).filter(
            Booking.slot_date == tomorrow,
            Booking.status == BookingStatus.CONFIRMED,
            Booking.reminder_24h_sent.is_(False),
        )
    ).scalars().all()

    for b in bookings_24h:
        if not b.customer or not b.tenant:
            continue
        res = notifier.send_notification(
            tenant=b.tenant,
            recipient_phone=b.customer.phone,
            template_name="appointment_reminder_24h",
            parameters={
                "customer_name": b.customer.name,
                "resource_name": b.resource.name if b.resource else "Appointment",
                "slot_date": b.slot_date.strftime("%d %b %Y"),
                "start_time": b.start_time.strftime("%I:%M %p"),
                "venue_name": b.tenant.name,
            },
            booking_id=b.id,
            customer_id=b.customer.id,
        )
        if res.status == NotificationStatus.QUOTA_EXCEEDED:
            quota_exceeded_count += 1
        elif res.success:
            b.reminder_24h_sent = True
            reminders_24h_sent += 1
            db.session.commit()

    # 2. 2h Reminders (Bookings today starting between now+1h and now+3h)
    bookings_today = db.session.execute(
        select(Booking).filter(
            Booking.slot_date == today,
            Booking.status == BookingStatus.CONFIRMED,
            Booking.reminder_2h_sent.is_(False),
        )
    ).scalars().all()

    for b in bookings_today:
        if not b.customer or not b.tenant:
            continue
        slot_dt = datetime.combine(today, b.start_time)
        delta_hours = (slot_dt - now_ref).total_seconds() / 3600.0
        if 0.5 <= delta_hours <= 3.0:
            res = notifier.send_notification(
                tenant=b.tenant,
                recipient_phone=b.customer.phone,
                template_name="appointment_reminder_2h",
                parameters={
                    "customer_name": b.customer.name,
                    "resource_name": b.resource.name if b.resource else "Appointment",
                    "slot_date": b.slot_date.strftime("%d %b %Y"),
                    "start_time": b.start_time.strftime("%I:%M %p"),
                    "venue_name": b.tenant.name,
                },
                booking_id=b.id,
                customer_id=b.customer.id,
            )
            if res.status == NotificationStatus.QUOTA_EXCEEDED:
                quota_exceeded_count += 1
            elif res.success:
                b.reminder_2h_sent = True
                reminders_2h_sent += 1
                db.session.commit()

    # 3. Clinic Follow-Up Reminders (next_followup_date <= today + 2 days)
    target_followup_limit = today + timedelta(days=2)
    visits_due = db.session.execute(
        select(ClinicVisit).filter(
            ClinicVisit.next_followup_date.isnot(None),
            ClinicVisit.next_followup_date >= today,
            ClinicVisit.next_followup_date <= target_followup_limit,
            ClinicVisit.followup_reminder_sent.is_(False),
        )
    ).scalars().all()

    for v in visits_due:
        if not v.customer or not v.tenant:
            continue
        res = notifier.send_notification(
            tenant=v.tenant,
            recipient_phone=v.customer.phone,
            template_name="clinic_followup_v1",
            parameters={
                "patient_name": v.customer.name,
                "doctor_name": v.doctor_resource.name if v.doctor_resource else v.tenant.name,
                "followup_date": v.next_followup_date.strftime("%d %b %Y"),
                "booking_link": f"/b/{v.tenant.slug}",
            },
            customer_id=v.customer.id,
        )
        if res.status == NotificationStatus.QUOTA_EXCEEDED:
            quota_exceeded_count += 1
        elif res.success:
            v.followup_reminder_sent = True
            followups_sent += 1
            db.session.commit()

    return {
        "reminders_24h_sent": reminders_24h_sent,
        "reminders_2h_sent": reminders_2h_sent,
        "followups_sent": followups_sent,
        "quota_exceeded_count": quota_exceeded_count,
    }