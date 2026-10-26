from datetime import timedelta
from decimal import Decimal
from io import StringIO
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from .models import ConsultationSession, RechargeTransaction, User, Wallet
from .services import bill_session


# A fast hasher keeps the suite quick; password security is not under test here.
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class BillingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="customer", password="password", role=User.Role.USER)
        self.provider = User.objects.create_user(
            username="expert", password="password", role=User.Role.PROVIDER, per_minute_rate=Decimal("50.00"),
        )
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

    def test_user_cannot_start_a_second_active_session(self):
        first = self.start(self.session_at())
        second = ConsultationSession.objects.create(
            user=self.user, provider=self.provider,
            status=ConsultationSession.Status.ACCEPTED, per_minute_cost=Decimal("50.00"),
        )
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{second.id}/start/")
        self.assertEqual(response.status_code, 400)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, ConsultationSession.Status.IN_PROGRESS)
        self.assertEqual(second.status, ConsultationSession.Status.ACCEPTED)

    def test_new_user_receives_wallet_and_database_blocks_negative_balance(self):
        another_user = User.objects.create_user(username="wallet-owner", password="password")
        self.assertEqual(another_user.wallet.balance, Decimal("0.00"))
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Wallet.objects.filter(pk=self.wallet.pk).update(balance=Decimal("-1.00"))
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("0.00"))

    def test_create_user_api_creates_provider_and_rejects_duplicate_username(self):
        missing_rate = self.client.post("/api/users/", {
            "username": "new-expert", "password": "safe-password", "role": "provider",
        }, format="json")
        self.assertEqual(missing_rate.status_code, 400)

        response = self.client.post("/api/users/", {
            "username": "new-expert", "password": "safe-password", "role": "provider", "per_minute_rate": "75.00",
        }, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["role"], User.Role.PROVIDER)
        self.assertEqual(response.data["per_minute_rate"], "75.00")
        self.assertTrue(User.objects.get(username="new-expert").check_password("safe-password"))

        duplicate = self.client.post("/api/users/", {
            "username": "new-expert", "password": "another-password", "role": "provider", "per_minute_rate": "75.00",
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
            "username": "api-provider", "password": "provider-password", "role": "provider", "per_minute_rate": "50.00",
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

        created = customer.post("/api/sessions/", format="json")
        self.assertEqual(created.status_code, 201)
        session_id = created.data["id"]
        self.assertIn(session_id, [s["id"] for s in provider.get("/api/sessions/").data])
        assigned = provider.post(f"/api/sessions/{session_id}/assign/")
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.data["per_minute_cost"], "50.00")
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
        self.assertEqual(session.actual_billable_minutes, 5)

    def test_assignment_example_wallet_stays_at_40_when_next_minute_is_unaffordable(self):
        # 290 at 50/minute: five minutes are paid, leaving 40, which cannot cover the sixth minute.
        session = self.start(self.session_at(balance="290.00"))
        session.started_at = timezone.now() - timedelta(minutes=5, seconds=1)
        session.save(update_fields=["started_at"])
        bill_session(session.id)
        self.wallet.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("40.00"))
        self.assertEqual(session.status, ConsultationSession.Status.COMPLETED)
        self.assertEqual(session.ended_by, ConsultationSession.EndedBy.SYSTEM)
        self.assertEqual(session.billed_minutes, 5)
        self.assertEqual(session.billed_amount, Decimal("250.00"))
        self.assertEqual(session.actual_billable_minutes, 5)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.post(f"/api/sessions/{session.id}/bill/").status_code, 400)

    def test_session_ends_before_an_unaffordable_minute_is_used(self):
        # After minute 5 the wallet holds 0, so the session must stop now rather than when minute 6 elapses.
        session = self.start(self.session_at(balance="250.00"))
        session.started_at = timezone.now() - timedelta(minutes=5, seconds=10)
        session.save(update_fields=["started_at"])
        bill_session(session.id)
        session.refresh_from_db()
        self.assertEqual(session.status, ConsultationSession.Status.COMPLETED)
        self.assertEqual(session.ended_by, ConsultationSession.EndedBy.SYSTEM)

    def test_partial_trailing_minute_is_not_charged(self):
        session = self.start(self.session_at())
        session.started_at = timezone.now() - timedelta(minutes=2, seconds=59)
        session.save(update_fields=["started_at"])
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{session.id}/end/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["billed_minutes"], 2)
        self.assertEqual(response.data["ended_by"], ConsultationSession.EndedBy.USER)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("400.00"))

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

    def test_rate_is_quoted_from_provider_and_locked_at_start(self):
        self.client.force_authenticate(self.user)
        session_id = self.client.post("/api/sessions/", format="json").data["id"]
        provider = APIClient()
        provider.force_authenticate(self.provider)
        self.assertEqual(provider.post(f"/api/sessions/{session_id}/assign/").data["per_minute_cost"], "50.00")
        self.assertEqual(provider.post(f"/api/sessions/{session_id}/accept/").status_code, 200)
        self.wallet.balance = Decimal("500.00")
        self.wallet.save()
        self.assertEqual(self.client.post(f"/api/sessions/{session_id}/start/").status_code, 200)
        self.provider.per_minute_rate = Decimal("80.00")
        self.provider.save()
        session = ConsultationSession.objects.get(pk=session_id)
        session.started_at = timezone.now() - timedelta(minutes=1, seconds=1)
        session.save(update_fields=["started_at"])
        bill_session(session_id)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("450.00"))

    def test_provider_without_rate_cannot_be_assigned(self):
        unpriced = User.objects.create_user(username="unpriced", password="password", role=User.Role.PROVIDER)
        session = ConsultationSession.objects.create(user=self.user)
        self.client.force_authenticate(unpriced)
        self.assertEqual(self.client.post(f"/api/sessions/{session.id}/assign/").status_code, 400)

    def test_provider_lists_unassigned_sessions_but_not_other_providers_sessions(self):
        unassigned = ConsultationSession.objects.create(user=self.user)
        other = User.objects.create_user(
            username="other", password="password", role=User.Role.PROVIDER, per_minute_rate=Decimal("10.00"),
        )
        taken = ConsultationSession.objects.create(
            user=self.user, provider=other, status=ConsultationSession.Status.ASSIGNED, per_minute_cost=Decimal("10.00"),
        )
        self.client.force_authenticate(self.provider)
        ids = [s["id"] for s in self.client.get("/api/sessions/").data]
        self.assertIn(unassigned.id, ids)
        self.assertNotIn(taken.id, ids)

    def test_accepting_an_unassigned_session_returns_400(self):
        session = ConsultationSession.objects.create(user=self.user)
        self.client.force_authenticate(self.provider)
        response = self.client.post(f"/api/sessions/{session.id}/accept/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("must be assigned", str(response.data["detail"]))

    def test_another_user_cannot_start_someone_elses_session(self):
        session = self.session_at()
        stranger = User.objects.create_user(username="stranger", password="password")
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.post(f"/api/sessions/{session.id}/start/").status_code, 403)

    def test_session_can_be_cancelled_before_start_but_not_after(self):
        pending = self.session_at()
        self.client.force_authenticate(self.user)
        response = self.client.post(f"/api/sessions/{pending.id}/cancel/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ConsultationSession.Status.CANCELLED)
        self.assertEqual(self.client.post(f"/api/sessions/{pending.id}/start/").status_code, 400)

        active = self.start(self.session_at())
        self.assertEqual(self.client.post(f"/api/sessions/{active.id}/cancel/").status_code, 400)

    def test_invalid_money_amounts_are_rejected(self):
        self.client.force_authenticate(self.user)
        for amount in ["0", "-5", "abc", "NaN", "1e20", "10.555", "1000000.01"]:
            response = self.client.post("/api/recharges/", {"amount": amount}, format="json")
            self.assertEqual(response.status_code, 400, amount)
        response = self.client.post("/api/recharges/", {"amount": "1e2"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["amount"], "100.00")

    def test_seed_demo_builds_consistent_data_through_services(self):
        call_command("seed_demo", stdout=StringIO())
        balances = {w.user.username: w.balance for w in Wallet.objects.filter(user__username__startswith="demo_")}
        self.assertEqual(balances["demo_customer"], Decimal("810.00"))
        self.assertEqual(balances["demo_runs_out"], Decimal("40.00"))
        ran_out = ConsultationSession.objects.get(user__username="demo_runs_out")
        self.assertEqual(ran_out.ended_by, ConsultationSession.EndedBy.SYSTEM)
        self.assertEqual(ran_out.actual_billable_minutes, 5)
        statuses = set(ConsultationSession.objects.filter(user__username__startswith="demo_").values_list("status", flat=True))
        self.assertEqual(statuses, set(ConsultationSession.Status.values) - {ConsultationSession.Status.IN_PROGRESS})
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())
        call_command("seed_demo", "--reset", stdout=StringIO())
        self.assertEqual(Wallet.objects.get(user__username="demo_customer").balance, Decimal("810.00"))
