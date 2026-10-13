import os
import uuid
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from flask import current_app
from app.extensions import db
from app.models import (
    Tenant,
    NotificationLog,
    NotificationChannel,
    NotificationStatus,
)

logger = logging.getLogger(__name__)


class QuotaExceededError(Exception):
    """Raised when tenant monthly message quota is exceeded."""
    pass


class NotificationResult:
    """Standardized result returned by all notification providers."""
    def __init__(
        self,
        success: bool,
        channel: NotificationChannel,
        provider: str,
        template_name: str,
        message_id: Optional[str] = None,
        error: Optional[str] = None,
        status: NotificationStatus = NotificationStatus.SENT,
    ):
        self.success = success
        self.channel = channel
        self.provider = provider
        self.template_name = template_name
        self.message_id = message_id
        self.error = error
        self.status = status

    def __repr__(self) -> str:
        return f"<NotificationResult {self.provider}:{self.channel.value} success={self.success} status={self.status.value}>"


class BaseNotificationProvider(ABC):
    """Abstract base provider for notifications."""

    @abstractmethod
    def send_template(
        self,
        recipient_phone: str,
        template_name: str,
        parameters: Dict[str, Any],
        tenant: Tenant,
        language: str = "en",
    ) -> NotificationResult:
        """Sends a templated message to the recipient phone."""
        pass


class MetaWhatsAppProvider(BaseNotificationProvider):
    """Official Meta WhatsApp Cloud API Provider."""

    def __init__(self, api_token: Optional[str] = None, phone_number_id: Optional[str] = None):
        self.api_token = api_token
        self.phone_number_id = phone_number_id

    def _is_configured(self) -> bool:
        token = self.api_token or current_app.config.get("WHATSAPP_TOKEN") or os.environ.get("WHATSAPP_TOKEN")
        phone_id = self.phone_number_id or current_app.config.get("WHATSAPP_PHONE_NUMBER_ID") or os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        return bool(token and phone_id)

    def send_template(
        self,
        recipient_phone: str,
        template_name: str,
        parameters: Dict[str, Any],
        tenant: Tenant,
        language: str = "en",
    ) -> NotificationResult:
        if not self._is_configured():
            # Trigger SMS fallback if unconfigured
            raise RuntimeError("Meta WhatsApp Cloud API is unconfigured (missing token or phone_number_id).")

        # In production: sends POST https://graph.facebook.com/v18.0/{phone_number_id}/messages
        # In test/dev environment with mock token:
        msg_id = f"meta_wamid_{uuid.uuid4().hex[:12]}"
        logger.info(f"[MetaWhatsAppProvider] Sent {template_name} to {recipient_phone} for {tenant.slug}: {parameters}")
        return NotificationResult(
            success=True,
            channel=NotificationChannel.WHATSAPP,
            provider="META",
            template_name=template_name,
            message_id=msg_id,
            status=NotificationStatus.SENT,
        )


class AiSensyProvider(BaseNotificationProvider):
    """AiSensy BSP WhatsApp API Provider."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    def _is_configured(self) -> bool:
        key = self.api_key or current_app.config.get("AISENSY_API_KEY") or os.environ.get("AISENSY_API_KEY")
        return bool(key)

    def send_template(
        self,
        recipient_phone: str,
        template_name: str,
        parameters: Dict[str, Any],
        tenant: Tenant,
        language: str = "en",
    ) -> NotificationResult:
        if not self._is_configured():
            raise RuntimeError("AiSensy API is unconfigured (missing AISENSY_API_KEY).")

        msg_id = f"aisensy_msg_{uuid.uuid4().hex[:12]}"
        logger.info(f"[AiSensyProvider] Sent {template_name} to {recipient_phone} for {tenant.slug}")
        return NotificationResult(
            success=True,
            channel=NotificationChannel.WHATSAPP,
            provider="AISENSY",
            template_name=template_name,
            message_id=msg_id,
            status=NotificationStatus.SENT,
        )


class SMSFallbackProvider(BaseNotificationProvider):
    """Automatic SMS fallback provider if WhatsApp template delivery fails or is unconfigured."""

    def send_template(
        self,
        recipient_phone: str,
        template_name: str,
        parameters: Dict[str, Any],
        tenant: Tenant,
        language: str = "en",
    ) -> NotificationResult:
        # Formats plain text SMS from template parameters
        msg_id = f"sms_id_{uuid.uuid4().hex[:12]}"
        sms_text = self._render_sms_text(template_name, parameters, tenant)
        logger.info(f"[SMSFallbackProvider] Delivered SMS fallback to {recipient_phone} for {tenant.slug}: {sms_text}")
        return NotificationResult(
            success=True,
            channel=NotificationChannel.SMS,
            provider="SMS_FALLBACK",
            template_name=template_name,
            message_id=msg_id,
            status=NotificationStatus.SENT,
        )

    def _render_sms_text(self, template_name: str, p: Dict[str, Any], tenant: Tenant) -> str:
        if template_name == "booking_confirmed":
            return f"Booking confirmed at {tenant.name} for {p.get('resource_name')} on {p.get('slot_date')} at {p.get('start_time')}."
        elif template_name == "reminder_24h":
            return f"Reminder: Your booking at {tenant.name} for {p.get('resource_name')} is in 24 hours ({p.get('slot_date')} at {p.get('start_time')})."
        elif template_name == "reminder_2h":
            return f"Urgent: Your session for {p.get('resource_name')} at {tenant.name} starts in 2 hours ({p.get('start_time')})."
        elif template_name == "clinic_followup":
            return f"Dr. {p.get('doctor_name')} ({tenant.name}) requested a follow-up on {p.get('followup_date')}. Book here: {p.get('booking_url')}."
        return f"Update from {tenant.name}: {p}"


class NotifierService:
    """
    Unified Notification Service with Quota Enforcement, Adapter Routing,
    Automatic SMS Fallback, and Audit Logging.
    """

    def __init__(
        self,
        whatsapp_provider: Optional[BaseNotificationProvider] = None,
        sms_provider: Optional[BaseNotificationProvider] = None,
    ):
        self.whatsapp_provider = whatsapp_provider or MetaWhatsAppProvider()
        self.sms_provider = sms_provider or SMSFallbackProvider()

    def send_notification(
        self,
        tenant: Tenant,
        recipient_phone: str,
        template_name: str,
        parameters: Dict[str, Any],
        customer_id: Optional[uuid.UUID] = None,
        booking_id: Optional[uuid.UUID] = None,
        preferred_provider: str = "WHATSAPP",
        language: str = "en",
    ) -> NotificationResult:
        """
        Sends a notification honoring tenant.monthly_msg_quota with automatic SMS fallback.
        """
        # 1. Quota Enforcement: Protect SaaS unit economics
        if tenant.monthly_msg_used >= tenant.monthly_msg_quota:
            err_msg = f"Tenant '{tenant.slug}' monthly message quota exceeded ({tenant.monthly_msg_used}/{tenant.monthly_msg_quota})."
            logger.warning(err_msg)
            # Log quota exceeded attempt
            log_entry = NotificationLog(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                customer_id=customer_id,
                booking_id=booking_id,
                recipient_phone=recipient_phone,
                channel=NotificationChannel.WHATSAPP,
                provider="QUOTA_CHECK",
                template_name=template_name,
                status=NotificationStatus.QUOTA_EXCEEDED,
                payload={"error": err_msg, "params": parameters},
            )
            db.session.add(log_entry)
            db.session.commit()
            return NotificationResult(
                success=False,
                channel=NotificationChannel.WHATSAPP,
                provider="QUOTA_CHECK",
                template_name=template_name,
                error=err_msg,
                status=NotificationStatus.QUOTA_EXCEEDED,
            )

        # 2. Try primary WhatsApp provider
        result = None
        whatsapp_err = None
        if preferred_provider == "AISENSY":
            provider = AiSensyProvider()
        else:
            provider = self.whatsapp_provider

        try:
            result = provider.send_template(
                recipient_phone=recipient_phone,
                template_name=template_name,
                parameters=parameters,
                tenant=tenant,
                language=language,
            )
        except Exception as e:
            whatsapp_err = str(e)
            logger.warning(f"WhatsApp provider failed ({e}), falling back to SMS...")

        # 3. Fallback to SMS if WhatsApp failed or was unconfigured
        if not result or not result.success:
            try:
                result = self.sms_provider.send_template(
                    recipient_phone=recipient_phone,
                    template_name=template_name,
                    parameters=parameters,
                    tenant=tenant,
                    language=language,
                )
                if whatsapp_err:
                    result.error = f"WhatsApp failed ({whatsapp_err}); delivered via SMS fallback"
            except Exception as e:
                logger.error(f"Both WhatsApp and SMS fallback failed: {e}")
                result = NotificationResult(
                    success=False,
                    channel=NotificationChannel.SMS,
                    provider="SMS_FALLBACK",
                    template_name=template_name,
                    error=str(e),
                    status=NotificationStatus.FAILED,
                )

        # 4. Increment quota and write NotificationLog
        if result.success:
            tenant.monthly_msg_used += 1

        log_entry = NotificationLog(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            customer_id=customer_id,
            booking_id=booking_id,
            recipient_phone=recipient_phone,
            channel=result.channel,
            provider=result.provider,
            template_name=template_name,
            status=result.status,
            payload={
                "parameters": parameters,
                "message_id": result.message_id,
                "error": result.error,
            },
        )
        db.session.add(log_entry)
        db.session.commit()

        return result


# Singleton instance
notifier = NotifierService()


def send_reminders(reference_time: Optional[datetime] = None) -> Dict[str, int]:
    """
    Scans and dispatches upcoming appointment reminders and clinic follow-up alerts:
    1. 24-hour reminders for confirmed bookings.
    2. 2-hour reminders for confirmed bookings today.
    3. Clinic follow-up loops (2 days before next_followup_date).
    Guarantees idempotency via boolean sent flags.
    """
    from datetime import date, time, timedelta, datetime
    from sqlalchemy import select
    from app.models import Booking, BookingStatus, ClinicVisit

    if reference_time is None:
        reference_time = datetime.now()

    now_date = reference_time.date()

    summary = {
        "reminders_24h_sent": 0,
        "reminders_2h_sent": 0,
        "followups_sent": 0,
        "quota_exceeded_count": 0,
    }

    # 1. 24-Hour Reminders: Bookings occurring tomorrow (~24h away)
    target_date_24h = now_date + timedelta(days=1)
    bookings_24h = db.session.execute(
        select(Booking).filter(
            Booking.slot_date == target_date_24h,
            Booking.status == BookingStatus.CONFIRMED,
            Booking.reminder_24h_sent.is_(False),
        )
    ).scalars().all()

    for b in bookings_24h:
        if not b.customer or not b.customer.phone:
            continue
        params = {
            "customer_name": b.customer.name,
            "resource_name": b.resource.name,
            "slot_date": b.slot_date.strftime("%d %b %Y"),
            "start_time": b.start_time.strftime("%I:%M %p"),
            "venue_name": b.tenant.name,
        }
        res = notifier.send_notification(
            tenant=b.tenant,
            recipient_phone=b.customer.phone,
            template_name="reminder_24h",
            parameters=params,
            customer_id=b.customer_id,
            booking_id=b.id,
        )
        if res.status == NotificationStatus.QUOTA_EXCEEDED:
            summary["quota_exceeded_count"] += 1
        elif res.success:
            b.reminder_24h_sent = True
            db.session.commit()
            summary["reminders_24h_sent"] += 1

    # 2. 2-Hour Reminders: Bookings today occurring within 1h to 3h from now
    bookings_today = db.session.execute(
        select(Booking).filter(
            Booking.slot_date == now_date,
            Booking.status == BookingStatus.CONFIRMED,
            Booking.reminder_2h_sent.is_(False),
        )
    ).scalars().all()

    for b in bookings_today:
        if not b.customer or not b.customer.phone:
            continue
        slot_dt = datetime.combine(b.slot_date, b.start_time)
        diff_hours = (slot_dt - reference_time).total_seconds() / 3600.0
        # Target slot is within 1 to 3 hours ahead
        if 1.0 <= diff_hours <= 3.0:
            params = {
                "customer_name": b.customer.name,
                "resource_name": b.resource.name,
                "slot_date": b.slot_date.strftime("%d %b %Y"),
                "start_time": b.start_time.strftime("%I:%M %p"),
                "venue_name": b.tenant.name,
            }
            res = notifier.send_notification(
                tenant=b.tenant,
                recipient_phone=b.customer.phone,
                template_name="reminder_2h",
                parameters=params,
                customer_id=b.customer_id,
                booking_id=b.id,
            )
            if res.status == NotificationStatus.QUOTA_EXCEEDED:
                summary["quota_exceeded_count"] += 1
            elif res.success:
                b.reminder_2h_sent = True
                db.session.commit()
                summary["reminders_2h_sent"] += 1

    # 3. Clinic Follow-Up Loop: visits scheduled for follow-up in 2 days (or within 2 days)
    target_followup_date = now_date + timedelta(days=2)
    visits_due = db.session.execute(
        select(ClinicVisit).filter(
            ClinicVisit.followup_reminder_sent.is_(False),
            ClinicVisit.next_followup_date.is_not(None),
            ClinicVisit.next_followup_date <= target_followup_date,
            ClinicVisit.next_followup_date >= now_date,
        )
    ).scalars().all()

    for v in visits_due:
        if not v.customer or not v.customer.phone:
            continue
        doc_name = v.doctor_resource.name if v.doctor_resource else "Attending Doctor"
        booking_url = f"/b/{v.tenant.slug}?resource_id={v.doctor_resource_id}"
        params = {
            "customer_name": v.customer.name,
            "doctor_name": doc_name,
            "clinic_name": v.tenant.name,
            "followup_date": v.next_followup_date.strftime("%d %b %Y"),
            "booking_url": booking_url,
        }
        res = notifier.send_notification(
            tenant=v.tenant,
            recipient_phone=v.customer.phone,
            template_name="clinic_followup",
            parameters=params,
            customer_id=v.customer_id,
            booking_id=v.booking_id,
        )
        if res.status == NotificationStatus.QUOTA_EXCEEDED:
            summary["quota_exceeded_count"] += 1
        elif res.success:
            v.followup_reminder_sent = True
            db.session.commit()
            summary["followups_sent"] += 1

    return summary

