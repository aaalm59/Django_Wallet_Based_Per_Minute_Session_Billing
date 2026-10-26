from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from .models import ConsultationSession, SessionBill, Wallet


def _fail(message):
    raise ValidationError({"detail": message})


@transaction.atomic
def start_session(session_id, user):
    session = ConsultationSession.objects.select_for_update().get(pk=session_id)
    if session.user_id != user.id or session.status != ConsultationSession.Status.ACCEPTED:
        _fail("Session must be accepted by its provider before the user can start it.")
    if ConsultationSession.objects.filter(user=user, status=ConsultationSession.Status.IN_PROGRESS).exists():
        _fail("A user may only have one in-progress session.")
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
    required = session.per_minute_cost * 5
    if wallet.balance < required:
        _fail("At least five minutes of balance is required to start a session.")
    session.locked_per_minute_cost = session.per_minute_cost
    session.status = ConsultationSession.Status.IN_PROGRESS
    session.started_at = timezone.now()
    session.save(update_fields=["locked_per_minute_cost", "status", "started_at"])
    return session


@transaction.atomic
def bill_session(session_id, now=None):
    session = ConsultationSession.objects.select_for_update().get(pk=session_id)
    if session.status != ConsultationSession.Status.IN_PROGRESS:
        _fail("Only an in-progress session can be billed.")
    now = now or timezone.now()
    elapsed = int((now - session.started_at).total_seconds() // 60)
    due_minutes = max(0, elapsed - session.billed_minutes)
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=session.user)
    rate = session.locked_per_minute_cost
    for _ in range(due_minutes):
        next_minute = session.billed_minutes + 1
        if wallet.balance < rate:
            session.status = ConsultationSession.Status.COMPLETED
            session.ended_at = now
            session.ended_by = ConsultationSession.EndedBy.SYSTEM
            session.actual_billable_minutes = session.billed_minutes
            session.save(update_fields=["status", "ended_at", "ended_by", "actual_billable_minutes", "billed_minutes", "billed_amount"])
            return session
        wallet.balance -= rate
        wallet.save(update_fields=["balance"])
        SessionBill.objects.create(session=session, minute_number=next_minute, amount=rate)
        session.billed_minutes = next_minute
        session.billed_amount += rate
    session.actual_billable_minutes = session.billed_minutes
    session.save(update_fields=["billed_minutes", "billed_amount", "actual_billable_minutes"])
    return session


@transaction.atomic
def end_session(session_id, actor, ended_by):
    session = ConsultationSession.objects.select_for_update().get(pk=session_id)
    if session.status != ConsultationSession.Status.IN_PROGRESS:
        _fail("Only an in-progress session can be ended.")
    if ended_by == ConsultationSession.EndedBy.USER and session.user_id != actor.id:
        _fail("Only the session user can use this endpoint.")
    if ended_by == ConsultationSession.EndedBy.PROVIDER and session.provider_id != actor.id:
        _fail("Only the assigned provider can use this endpoint.")
    # Settle every complete elapsed minute before closing.
    bill_session(session_id)
    session.refresh_from_db()
    if session.status == ConsultationSession.Status.IN_PROGRESS:
        session.status = ConsultationSession.Status.COMPLETED
        session.ended_at = timezone.now()
        session.ended_by = ended_by
        session.actual_billable_minutes = session.billed_minutes
        session.save(update_fields=["status", "ended_at", "ended_by", "actual_billable_minutes"])
    return session
