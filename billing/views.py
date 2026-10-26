import uuid
from django.db import transaction
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from .models import ConsultationSession, RechargeTransaction, User, Wallet
from .serializers import RechargeSerializer, SessionSerializer, WalletSerializer
from .services import bill_session, end_session, start_session


class WalletView(APIView):
    def get(self, request):
        wallet, _ = Wallet.objects.get_or_create(user=request.user)
        return Response(WalletSerializer(wallet).data)


class RechargeInitiateView(APIView):
    def post(self, request):
        serializer = RechargeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        recharge = serializer.save(user=request.user, reference=uuid.uuid4().hex)
        return Response(RechargeSerializer(recharge).data, status=status.HTTP_201_CREATED)


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
        return Response(RechargeSerializer(recharge).data)


class SessionListCreateView(APIView):
    def get(self, request):
        sessions = (ConsultationSession.objects.filter(user=request.user)
                    if request.user.role == User.Role.USER
                    else ConsultationSession.objects.filter(provider=request.user))
        return Response(SessionSerializer(sessions, many=True).data)

    def post(self, request):
        if request.user.role != User.Role.USER:
            raise PermissionDenied("Only users can create consultations.")
        serializer = SessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = serializer.save(user=request.user)
        return Response(SessionSerializer(session).data, status=status.HTTP_201_CREATED)


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
        return Response(SessionSerializer(session).data)


class SessionAcceptView(APIView):
    def post(self, request, pk):
        with transaction.atomic():
            session = ConsultationSession.objects.select_for_update().get(pk=pk)
            if session.provider_id != request.user.id or session.status != ConsultationSession.Status.ASSIGNED:
                raise ValidationError({"detail": "Only the assigned provider can accept an assigned session."})
            session.status = ConsultationSession.Status.ACCEPTED
            session.save(update_fields=["status"])
        return Response(SessionSerializer(session).data)


class SessionStartView(APIView):
    def post(self, request, pk):
        return Response(SessionSerializer(start_session(pk, request.user)).data)


class SessionBillView(APIView):
    def post(self, request, pk):
        session = ConsultationSession.objects.get(pk=pk)
        if request.user.id not in (session.user_id, session.provider_id):
            raise PermissionDenied("You are not part of this session.")
        return Response(SessionSerializer(bill_session(pk)).data)


class SessionEndView(APIView):
    def post(self, request, pk):
        return Response(SessionSerializer(end_session(pk, request.user, ConsultationSession.EndedBy.USER)).data)


class ProviderSessionEndView(APIView):
    def post(self, request, pk):
        return Response(SessionSerializer(end_session(pk, request.user, ConsultationSession.EndedBy.PROVIDER)).data)
