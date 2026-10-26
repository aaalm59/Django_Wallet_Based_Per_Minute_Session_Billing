from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from .models import ConsultationSession, RechargeTransaction, User, Wallet
from .services import bill_session


class BillingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="customer", password="password", role=User.Role.USER)
        self.provider = User.objects.create_user(username="expert", password="password", role=User.Role.PROVIDER)
        self.wallet = Wallet.objects.create(user=self.user, balance=Decimal("0.00"))
        self.client = APIClient()

    def session_at(self, status=ConsultationSession.Status.ACCEPTED, balance="500.00"):
        self.wallet.balance = Decimal(balance)
        self.wallet.save()
        session = ConsultationSession.objects.create(user=self.user, provider=self.provider, status=status, per_minute_cost=Decimal("50.00"))
        return session

    def start(self, session):
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{session.id}/start/")
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        return session

    def test_insufficient_balance_cannot_start(self):
        session = self.session_at(balance="249.99")
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{session.id}/start/")
        self.assertEqual(response.status_code, 400)
        session.refresh_from_db()
        self.assertEqual(session.status, ConsultationSession.Status.ACCEPTED)

    def test_happy_path_bills_two_minutes_and_ends(self):
        session = self.start(self.session_at())
        session.started_at = timezone.now() - timedelta(minutes=2, seconds=1)
        session.save(update_fields=["started_at"])
        bill_session(session.id)
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{session.id}/end/")
        self.assertEqual(response.status_code, 200)
        self.wallet.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("400.00"))
        self.assertEqual(session.billed_minutes, 2)
        self.assertEqual(session.status, ConsultationSession.Status.COMPLETED)

    def test_balance_runs_out_without_going_negative(self):
        session = self.start(self.session_at(balance="250.00"))
        session.started_at = timezone.now() - timedelta(minutes=6)
        session.save(update_fields=["started_at"])
        bill_session(session.id)
        self.wallet.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("0.00"))
        self.assertEqual(session.status, ConsultationSession.Status.COMPLETED)
        self.assertEqual(session.ended_by, ConsultationSession.EndedBy.SYSTEM)
        self.assertEqual(session.billed_minutes, 5)

    def test_invalid_transition_returns_400(self):
        session = self.session_at(status=ConsultationSession.Status.ASSIGNED)
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{session.id}/end/")
        self.assertEqual(response.status_code, 400)

    def test_duplicate_success_callback_credits_once(self):
        recharge = RechargeTransaction.objects.create(user=self.user, amount=Decimal("100.00"), reference="payment-1")
        response = self.client.post(f"/api/recharges/{recharge.reference}/callback/", {"status": "success"}, format="json")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f"/api/recharges/{recharge.reference}/callback/", {"status": "success"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("100.00"))

    def test_duplicate_billing_does_not_double_charge_a_minute(self):
        session = self.start(self.session_at())
        session.started_at = timezone.now() - timedelta(minutes=2)
        session.save(update_fields=["started_at"])
        bill_session(session.id)
        bill_session(session.id)
        self.wallet.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(session.billed_minutes, 2)
        self.assertEqual(self.wallet.balance, Decimal("400.00"))
