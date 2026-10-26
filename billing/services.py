import uuid
from decimal import Decimal
from django.shortcuts import get_object_or_404
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from .models import ConsultationSession, RechargeTransaction, SessionBill, User, Wallet

# Keep every amount well inside the DecimalField limits so the database can never overflow.
MAX_RECHARGE_AMOUNT = Decimal("1000000.00")
MAX_PER_MINUTE_RATE = Decimal("100000.00")
MAX_WALLET_BALANCE = Decimal("9999999999.99")

CANCELLABLE_STATUSES = (
    ConsultationSession.Status.UNASSIGNED,
    ConsultationSession.Status.ASSIGNED,
    ConsultationSession.Status.ACCEPTED,
)


def _fail(message):
    raise ValidationError({"detail": message})


def _locked_session(session_id):
    return get_object_or_404(ConsultationSession.objects.select_for_update(), pk=session_id)


def _locked_wallet(user):
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
    return wallet


def initiate_recharge(user, amount):
    wallet, _ = Wallet.objects.get_or_create(user=user)
    if wallet.balance + amount > MAX_WALLET_BALANCE:
        _fail(f"Wallet balance cannot exceed {MAX_WALLET_BALANCE}.")
    return RechargeTransaction.objects.create(user=user, amount=amount, reference=uuid.uuid4().hex)


@transaction.atomic
def complete_recharge(reference, requested_status):
    recharge = get_object_or_404(RechargeTransaction.objects.select_for_update(), reference=reference)
    # Gateway outcomes are final. Only a pending transaction may change state or credit money.
    if recharge.status != RechargeTransaction.Status.PENDING:
        return recharge
    recharge.status = requested_status
    recharge.save(update_fields=["status"])
    if requested_status == RechargeTransaction.Status.SUCCESS:
        wallet = _locked_wallet(recharge.user)
        if wallet.balance + recharge.amount > MAX_WALLET_BALANCE:
            _fail(f"Wallet balance cannot exceed {MAX_WALLET_BALANCE}.")
        wallet.balance += recharge.amount
        wallet.save(update_fields=["balance"])
    return recharge


@transaction.atomic
def assign_session(session_id, provider):
    if provider.role != User.Role.PROVIDER:
        raise PermissionDenied("Only providers can be assigned.")
    session = _locked_session(session_id)
    if session.status != ConsultationSession.Status.UNASSIGNED:
        _fail(f"Cannot assign a session that is {session.status}; it must be unassigned.")
    if provider.per_minute_rate is None:
        _fail("Set a per-minute rate on your provider account before taking sessions.")
    session.provider = provider
    session.per_minute_cost = provider.per_minute_rate
    session.status = ConsultationSession.Status.ASSIGNED
    session.save(update_fields=["provider", "per_minute_cost", "status"])
    return session


@transaction.atomic
def accept_session(session_id, provider):
    session = _locked_session(session_id)
    if session.status != ConsultationSession.Status.ASSIGNED:
        _fail(f"Cannot accept a session that is {session.status}; it must be assigned.")
    if session.provider_id != provider.id:
        raise PermissionDenied("Only the assigned provider can accept this session.")
    session.status = ConsultationSession.Status.ACCEPTED
    session.save(update_fields=["status"])
    return session


@transaction.atomic
def start_session(session_id, user):
    session = _locked_session(session_id)
    if session.user_id != user.id:
        raise PermissionDenied("Only the session user can start this session.")
    if session.status != ConsultationSession.Status.ACCEPTED:
        _fail(f"Cannot start a session that is {session.status}; it must be accepted_by_provider.")
    if ConsultationSession.objects.filter(user=user, status=ConsultationSession.Status.IN_PROGRESS).exists():
        _fail("A user may only have one in-progress session.")
    wallet = _locked_wallet(user)
    required = session.per_minute_cost * 5
    if wallet.balance < required:
        _fail(f"At least five minutes of balance ({required}) is required to start this session.")
    try:
        # The nested transaction lets us translate a concurrent unique-constraint failure to HTTP 400.
        with transaction.atomic():
            session.locked_per_minute_cost = session.per_minute_cost
            session.status = ConsultationSession.Status.IN_PROGRESS
            session.started_at = timezone.now()
            session.save(update_fields=["locked_per_minute_cost", "status", "started_at"])
    except IntegrityError:
        _fail("A user may only have one in-progress session.")
    return session


def _charge_due_minutes(session, wallet, now):
    """Charge every complete elapsed minute not yet billed. Returns False if a due minute cannot be paid."""
    elapsed = int((now - session.started_at).total_seconds() // 60)
    rate = session.locked_per_minute_cost
    for minute in range(session.billed_minutes + 1, elapsed + 1):
        if wallet.balance < rate:
            return False
        wallet.balance -= rate
        wallet.save(update_fields=["balance"])
        SessionBill.objects.create(session=session, minute_number=minute, amount=rate)
        session.billed_minutes = minute
        session.billed_amount += rate
    return True


def _complete(session, now, ended_by):
    session.status = ConsultationSession.Status.COMPLETED
    session.ended_at = now
    session.ended_by = ended_by
    session.actual_billable_minutes = session.billed_minutes
    session.save(update_fields=["status", "ended_at", "ended_by", "billed_minutes", "billed_amount", "actual_billable_minutes"])
    return session


@transaction.atomic
def bill_session(session_id, now=None):
    session = _locked_session(session_id)
    if session.status != ConsultationSession.Status.IN_PROGRESS:
        _fail(f"Cannot bill a session that is {session.status}; it must be in_progress.")
    now = now or timezone.now()
    wallet = _locked_wallet(session.user)
    paid_all = _charge_due_minutes(session, wallet, now)
    # End as soon as the wallet cannot cover the minute that is now running, so no minute is ever unpaid.
    if not paid_all or wallet.balance < session.locked_per_minute_cost:
        return _complete(session, now, ConsultationSession.EndedBy.SYSTEM)
    session.actual_billable_minutes = session.billed_minutes
    session.save(update_fields=["billed_minutes", "billed_amount", "actual_billable_minutes"])
    return session


@transaction.atomic
def end_session(session_id, actor, ended_by):
    session = _locked_session(session_id)
    if ended_by == ConsultationSession.EndedBy.USER and session.user_id != actor.id:
        raise PermissionDenied("Only the session user can use this endpoint.")
    if ended_by == ConsultationSession.EndedBy.PROVIDER and session.provider_id != actor.id:
        raise PermissionDenied("Only the assigned provider can use this endpoint.")
    if session.status != ConsultationSession.Status.IN_PROGRESS:
        _fail(f"Cannot end a session that is {session.status}; it must be in_progress.")
    now = timezone.now()
    # Settle every complete elapsed minute before closing; a trailing partial minute is not charged.
    paid_all = _charge_due_minutes(session, _locked_wallet(session.user), now)
    return _complete(session, now, ended_by if paid_all else ConsultationSession.EndedBy.SYSTEM)


@transaction.atomic
def cancel_session(session_id, actor):
    session = _locked_session(session_id)
    if actor.id not in (session.user_id, session.provider_id):
        raise PermissionDenied("You are not part of this session.")
    if session.status not in CANCELLABLE_STATUSES:
        _fail(f"Cannot cancel a session that is {session.status}; only sessions that have not started can be cancelled.")
    session.status = ConsultationSession.Status.CANCELLED
    session.ended_at = timezone.now()
    session.ended_by = (ConsultationSession.EndedBy.USER if actor.id == session.user_id
                        else ConsultationSession.EndedBy.PROVIDER)
    session.save(update_fields=["status", "ended_at", "ended_by"])
    return session
