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
        self.wallet = Wallet.objects.get(user=self.user)
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

    def test_create_user_api_creates_provider_and_rejects_duplicate_username(self):
        response = self.client.post("/api/users/", {
            "username": "new-expert", "password": "safe-password", "role": "provider",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["role"], User.Role.PROVIDER)
        self.assertTrue(User.objects.get(username="new-expert").check_password("safe-password"))

        duplicate = self.client.post("/api/users/", {
            "username": "new-expert", "password": "another-password", "role": "provider",
        }, format="json")
        self.assertEqual(duplicate.status_code, 400)

    def test_jwt_token_authenticates_protected_wallet_api(self):
        token_response = self.client.post("/api/token/", {
            "username": "customer", "password": "password",
        }, format="json")
        self.assertEqual(token_response.status_code, 200)
        self.assertIn("access", token_response.data)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_response.data['access']}")
        wallet_response = self.client.get("/api/wallet/")
        self.assertEqual(wallet_response.status_code, 200)
        self.assertEqual(wallet_response.data["balance"], "0.00")

    def test_complete_jwt_api_flow(self):
        customer_signup = self.client.post("/api/users/", {
            "username": "api-customer", "password": "customer-password", "role": "user",
        }, format="json")
        provider_signup = self.client.post("/api/users/", {
            "username": "api-provider", "password": "provider-password", "role": "provider",
        }, format="json")
        self.assertEqual(customer_signup.status_code, 201)
        self.assertEqual(provider_signup.status_code, 201)

        def token_for(username, password):
            response = self.client.post("/api/token/", {"username": username, "password": password}, format="json")
            self.assertEqual(response.status_code, 200)
            refresh = self.client.post("/api/token/refresh/", {"refresh": response.data["refresh"]}, format="json")
            self.assertEqual(refresh.status_code, 200)
            return response.data["access"]

        customer = APIClient()
        customer.credentials(HTTP_AUTHORIZATION=f"Bearer {token_for('api-customer', 'customer-password')}")
        provider = APIClient()
        provider.credentials(HTTP_AUTHORIZATION=f"Bearer {token_for('api-provider', 'provider-password')}")

        self.assertEqual(customer.get("/api/wallet/").status_code, 200)
        recharge = customer.post("/api/recharges/", {"amount": "500.00"}, format="json")
        self.assertEqual(recharge.status_code, 201)
        reference = recharge.data["reference"]
        self.assertEqual(self.client.post(f"/api/recharges/{reference}/callback/", {"status": "success"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/recharges/{reference}/callback/", {"status": "success"}, format="json").status_code, 200)

        created = customer.post("/api/sessions/", {"per_minute_cost": "50.00"}, format="json")
        self.assertEqual(created.status_code, 201)
        session_id = created.data["id"]
        self.assertEqual(provider.post(f"/api/sessions/{session_id}/assign/").status_code, 200)
        self.assertEqual(provider.post(f"/api/sessions/{session_id}/accept/").status_code, 200)
        self.assertEqual(customer.post(f"/api/sessions/{session_id}/start/").status_code, 200)

        session = ConsultationSession.objects.get(pk=session_id)
        session.started_at = timezone.now() - timedelta(minutes=2, seconds=1)
        session.save(update_fields=["started_at"])
        billed = customer.post(f"/api/sessions/{session_id}/bill/")
        self.assertEqual(billed.status_code, 200)
        self.assertEqual(billed.data["billed_minutes"], 2)
        ended = provider.post(f"/api/sessions/{session_id}/provider-end/")
        self.assertEqual(ended.status_code, 200)
        self.assertEqual(ended.data["status"], ConsultationSession.Status.COMPLETED)
        self.assertEqual(ended.data["ended_by"], ConsultationSession.EndedBy.PROVIDER)
        self.assertEqual(customer.get("/api/wallet/").data["balance"], "400.00")

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

    def test_failed_recharge_cannot_later_credit_wallet(self):
        recharge = RechargeTransaction.objects.create(user=self.user, amount=Decimal("100.00"), reference="payment-failed")
        self.assertEqual(self.client.post(f"/api/recharges/{recharge.reference}/callback/", {"status": "failed"}, format="json").status_code, 200)
        self.assertEqual(self.client.post(f"/api/recharges/{recharge.reference}/callback/", {"status": "success"}, format="json").status_code, 200)
        recharge.refresh_from_db()
        self.wallet.refresh_from_db()
        self.assertEqual(recharge.status, RechargeTransaction.Status.FAILED)
        self.assertEqual(self.wallet.balance, Decimal("0.00"))

    def test_unknown_session_returns_404(self):
        self.client.force_authenticate(self.user)
        response = self.client.post("/api/sessions/999999/start/")
        self.assertEqual(response.status_code, 404)

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
