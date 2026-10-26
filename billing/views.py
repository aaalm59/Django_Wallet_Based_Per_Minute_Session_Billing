from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import ConsultationSession, RechargeTransaction, User, Wallet
from .services import (MAX_PER_MINUTE_RATE, MAX_RECHARGE_AMOUNT, accept_session, assign_session, bill_session,
                       cancel_session, complete_recharge, end_session, initiate_recharge, start_session)


def positive_amount(data, field, max_value):
    try:
        amount = Decimal(str(data.get(field)))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError({field: "Provide a valid positive decimal amount."})
    if not amount.is_finite() or amount <= 0:
        raise ValidationError({field: "Must be greater than zero."})
    if amount > max_value:
        raise ValidationError({field: f"Must not exceed {max_value}."})
    if amount != amount.quantize(Decimal("0.01")):
        raise ValidationError({field: "Use at most two decimal places."})
    return amount.quantize(Decimal("0.01"))


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
        "status": session.status,
        "per_minute_cost": str(session.per_minute_cost) if session.per_minute_cost is not None else None,
        "locked_per_minute_cost": (str(session.locked_per_minute_cost)
                                  if session.locked_per_minute_cost is not None else None),
        "started_at": session.started_at, "ended_at": session.ended_at,
        "ended_by": session.ended_by, "billed_minutes": session.billed_minutes,
        "billed_amount": str(session.billed_amount),
        "actual_billable_minutes": session.actual_billable_minutes, "created_at": session.created_at,
    }


def user_data(user):
    return {
        "id": user.id, "username": user.username, "role": user.role,
        "per_minute_rate": str(user.per_minute_rate) if user.per_minute_rate is not None else None,
    }


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
        # Providers set their own price; customers never carry a rate.
        rate = (positive_amount(request.data, "per_minute_rate", MAX_PER_MINUTE_RATE)
                if role == User.Role.PROVIDER else None)
        user = User(username=username, role=role, per_minute_rate=rate)
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
        recharge = initiate_recharge(request.user, positive_amount(request.data, "amount", MAX_RECHARGE_AMOUNT))
        return Response(recharge_data(recharge), status=status.HTTP_201_CREATED)


class RechargeCallbackView(APIView):
    """Mock gateway callback. Repeating a success callback is safe."""
    permission_classes = [AllowAny]
    def post(self, request, reference):
        requested_status = request.data.get("status")
        if requested_status not in RechargeTransaction.Status.values:
            raise ValidationError({"status": "Use pending, success, or failed."})
        return Response(recharge_data(complete_recharge(reference, requested_status)))


class SessionListCreateView(APIView):
    def get(self, request):
        # Providers see their own sessions plus the unassigned ones they may pick up.
        sessions = (ConsultationSession.objects.filter(user=request.user)
                    if request.user.role == User.Role.USER
                    else ConsultationSession.objects.filter(
                        Q(provider=request.user) | Q(status=ConsultationSession.Status.UNASSIGNED)))
        sessions = sessions.order_by("-created_at")
        return Response([session_data(session) for session in sessions])

    def post(self, request):
        if request.user.role != User.Role.USER:
            raise PermissionDenied("Only users can create consultations.")
        # The rate is quoted from the provider at assignment, not chosen by the customer.
        session = ConsultationSession.objects.create(user=request.user)
        return Response(session_data(session), status=status.HTTP_201_CREATED)


class SessionAssignView(APIView):
    def post(self, request, pk):
        return Response(session_data(assign_session(pk, request.user)))


class SessionAcceptView(APIView):
    def post(self, request, pk):
        return Response(session_data(accept_session(pk, request.user)))


class SessionStartView(APIView):
    def post(self, request, pk):
        return Response(session_data(start_session(pk, request.user)))


class SessionBillView(APIView):
    def post(self, request, pk):
        session = get_object_or_404(ConsultationSession, pk=pk)
        if request.user.id not in (session.user_id, session.provider_id):
            raise PermissionDenied("You are not part of this session.")
        return Response(session_data(bill_session(pk)))


class SessionEndView(APIView):
    def post(self, request, pk):
        return Response(session_data(end_session(pk, request.user, ConsultationSession.EndedBy.USER)))


class ProviderSessionEndView(APIView):
    def post(self, request, pk):
        return Response(session_data(end_session(pk, request.user, ConsultationSession.EndedBy.PROVIDER)))


class SessionCancelView(APIView):
    def post(self, request, pk):
        return Response(session_data(cancel_session(pk, request.user)))
