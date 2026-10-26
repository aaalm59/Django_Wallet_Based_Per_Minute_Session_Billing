import uuid
from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import ConsultationSession, RechargeTransaction, User, Wallet
from .services import bill_session, end_session, start_session


def positive_amount(data, field):
    try:
        amount = Decimal(str(data.get(field)))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field: "Provide a valid positive decimal amount."})
    if not amount.is_finite() or amount <= 0:
        raise ValidationError({field: "Must be greater than zero."})
    return amount


def wallet_data(wallet):
    return {"balance": str(wallet.balance)}


def recharge_data(recharge):
    return {
        "id": recharge.id, "amount": str(recharge.amount), "reference": recharge.reference,
        "status": recharge.status, "created_at": recharge.created_at,
    }


def session_data(session):
    return {
        "id": session.id, "user": session.user_id, "provider": session.provider_id,
        "status": session.status, "per_minute_cost": str(session.per_minute_cost),
        "locked_per_minute_cost": (str(session.locked_per_minute_cost)
                                  if session.locked_per_minute_cost is not None else None),
        "started_at": session.started_at, "ended_at": session.ended_at,
        "ended_by": session.ended_by, "billed_minutes": session.billed_minutes,
        "billed_amount": str(session.billed_amount),
        "actual_billable_minutes": session.actual_billable_minutes, "created_at": session.created_at,
    }


def user_data(user):
    return {"id": user.id, "username": user.username, "role": user.role}


class UserCreateView(APIView):
    """Public registration endpoint for the two roles used by this assignment."""
    permission_classes = [AllowAny]

    def post(self, request):
        username = str(request.data.get("username", "")).strip()
        password = request.data.get("password")
        role = request.data.get("role", User.Role.USER)
        if not username:
            raise ValidationError({"username": "This field is required."})
        if not isinstance(password, str) or not password:
            raise ValidationError({"password": "This field is required."})
        if role not in User.Role.values:
            raise ValidationError({"role": "Use user or provider."})
        user = User(username=username, role=role)
        try:
            user.full_clean(exclude=["password"])
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict)
        user.set_password(password)
        user.save()
        return Response(user_data(user), status=status.HTTP_201_CREATED)


class WalletView(APIView):
    def get(self, request):
        wallet, _ = Wallet.objects.get_or_create(user=request.user)
        return Response(wallet_data(wallet))


class RechargeInitiateView(APIView):
    def post(self, request):
        recharge = RechargeTransaction.objects.create(
            user=request.user, amount=positive_amount(request.data, "amount"), reference=uuid.uuid4().hex
        )
        return Response(recharge_data(recharge), status=status.HTTP_201_CREATED)


class RechargeCallbackView(APIView):
    """Mock gateway callback. Repeating a success callback is safe."""
    permission_classes = [AllowAny]
    def post(self, request, reference):
        requested_status = request.data.get("status")
        if requested_status not in RechargeTransaction.Status.values:
            raise ValidationError({"status": "Use pending, success, or failed."})
        with transaction.atomic():
            recharge = RechargeTransaction.objects.select_for_update().get(reference=reference)
            if requested_status == RechargeTransaction.Status.SUCCESS and recharge.status != RechargeTransaction.Status.SUCCESS:
                wallet, _ = Wallet.objects.select_for_update().get_or_create(user=recharge.user)
                wallet.balance += recharge.amount
                wallet.save(update_fields=["balance"])
            if recharge.status == RechargeTransaction.Status.PENDING:
                recharge.status = requested_status
                recharge.save(update_fields=["status"])
        return Response(recharge_data(recharge))


class SessionListCreateView(APIView):
    def get(self, request):
        sessions = (ConsultationSession.objects.filter(user=request.user)
                    if request.user.role == User.Role.USER
                    else ConsultationSession.objects.filter(provider=request.user))
        return Response([session_data(session) for session in sessions])

    def post(self, request):
        if request.user.role != User.Role.USER:
            raise PermissionDenied("Only users can create consultations.")
        session = ConsultationSession.objects.create(
            user=request.user, per_minute_cost=positive_amount(request.data, "per_minute_cost")
        )
        return Response(session_data(session), status=status.HTTP_201_CREATED)


class SessionAssignView(APIView):
    def post(self, request, pk):
        if request.user.role != User.Role.PROVIDER:
            raise PermissionDenied("Only providers can be assigned.")
        with transaction.atomic():
            session = ConsultationSession.objects.select_for_update().get(pk=pk)
            if session.status != ConsultationSession.Status.UNASSIGNED:
                raise ValidationError({"detail": "Only an unassigned session may be assigned."})
            session.provider = request.user
            session.status = ConsultationSession.Status.ASSIGNED
            session.save(update_fields=["provider", "status"])
        return Response(session_data(session))


class SessionAcceptView(APIView):
    def post(self, request, pk):
        with transaction.atomic():
            session = ConsultationSession.objects.select_for_update().get(pk=pk)
            if session.provider_id != request.user.id or session.status != ConsultationSession.Status.ASSIGNED:
                raise ValidationError({"detail": "Only the assigned provider can accept an assigned session."})
            session.status = ConsultationSession.Status.ACCEPTED
            session.save(update_fields=["status"])
        return Response(session_data(session))


class SessionStartView(APIView):
    def post(self, request, pk):
        return Response(session_data(start_session(pk, request.user)))


class SessionBillView(APIView):
    def post(self, request, pk):
        session = ConsultationSession.objects.get(pk=pk)
        if request.user.id not in (session.user_id, session.provider_id):
            raise PermissionDenied("You are not part of this session.")
        return Response(session_data(bill_session(pk)))


class SessionEndView(APIView):
    def post(self, request, pk):
        return Response(session_data(end_session(pk, request.user, ConsultationSession.EndedBy.USER)))


class ProviderSessionEndView(APIView):
    def post(self, request, pk):
        return Response(session_data(end_session(pk, request.user, ConsultationSession.EndedBy.PROVIDER)))
