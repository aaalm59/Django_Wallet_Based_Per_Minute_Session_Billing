from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from billing.models import ConsultationSession, RechargeTransaction, User
from billing.services import (accept_session, assign_session, bill_session, cancel_session, complete_recharge,
                              end_session, initiate_recharge, start_session)

DEMO_PASSWORD = "demo1234"
DEMO_PREFIX = "demo_"


class Command(BaseCommand):
    help = "Create demo users, recharges and sessions covering every lifecycle path, using the real services."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing demo_* data before seeding.")

    def handle(self, *args, **options):
        demo_users = User.objects.filter(username__startswith=DEMO_PREFIX)
        if demo_users.exists():
            if not options["reset"]:
                raise CommandError("Demo data already exists. Re-run with --reset to recreate it.")
            with transaction.atomic():
                ConsultationSession.objects.filter(Q(user__in=demo_users) | Q(provider__in=demo_users)).delete()
                demo_users.delete()
        with transaction.atomic():
            self.seed()
        self.report()

    def user(self, username, role=User.Role.USER, rate=None):
        return User.objects.create_user(DEMO_PREFIX + username, password=DEMO_PASSWORD, role=role, per_minute_rate=rate)

    def recharge(self, user, amount, *callbacks):
        recharge = initiate_recharge(user, Decimal(amount))
        for callback_status in callbacks:
            complete_recharge(recharge.reference, callback_status)
        return recharge

    def ready_session(self, user, provider):
        session = ConsultationSession.objects.create(user=user)
        assign_session(session.id, provider)
        accept_session(session.id, provider)
        return session

    def run_for(self, session, minutes, seconds=0):
        """Pretend the session started earlier so elapsed minutes become billable."""
        ConsultationSession.objects.filter(pk=session.pk).update(
            started_at=timezone.now() - timedelta(minutes=minutes, seconds=seconds)
        )

    def seed(self):
        customer = self.user("customer")
        low_balance = self.user("low_balance")
        runs_out = self.user("runs_out")
        provider = self.user("provider", User.Role.PROVIDER, Decimal("50.00"))
        budget_provider = self.user("budget_provider", User.Role.PROVIDER, Decimal("30.00"))

        # Recharges: duplicate success callback credits once, failed and pending never credit.
        self.recharge(customer, "1000.00", "success", "success")
        self.recharge(customer, "500.00", "failed", "success")
        self.recharge(customer, "200.00")
        self.recharge(low_balance, "100.00", "success")
        self.recharge(runs_out, "290.00", "success")

        # Happy path: 2 minutes 30 seconds, billed twice, ended by the user -> 2 minutes charged.
        happy = self.ready_session(customer, provider)
        start_session(happy.id, customer)
        self.run_for(happy, 2, 30)
        bill_session(happy.id)
        bill_session(happy.id)
        end_session(happy.id, customer, ConsultationSession.EndedBy.USER)

        # Provider ends after 3 minutes at the budget rate.
        provider_ended = self.ready_session(customer, budget_provider)
        start_session(provider_ended.id, customer)
        self.run_for(provider_ended, 3, 5)
        end_session(provider_ended.id, budget_provider, ConsultationSession.EndedBy.PROVIDER)

        # Balance runs out: 290 at 50/minute pays 5 minutes and stops with 40 left.
        ran_out = self.ready_session(runs_out, provider)
        start_session(ran_out.id, runs_out)
        self.run_for(ran_out, 7)
        bill_session(ran_out.id)

        # Sessions parked at every pre-start state, plus a cancellation.
        self.ready_session(low_balance, provider)  # accepted, but 100 < 5 x 50 so it cannot start
        self.ready_session(customer, provider)  # accepted and ready to start
        assigned = ConsultationSession.objects.create(user=customer)
        assign_session(assigned.id, budget_provider)
        ConsultationSession.objects.create(user=customer)
        cancelled = ConsultationSession.objects.create(user=customer)
        assign_session(cancelled.id, provider)
        cancel_session(cancelled.id, customer)

    def report(self):
        out = self.stdout
        out.write(self.style.SUCCESS(f"Demo data created. Every demo account uses the password '{DEMO_PASSWORD}'."))
        out.write("\nUsers")
        for user in User.objects.filter(username__startswith=DEMO_PREFIX).select_related("wallet").order_by("id"):
            rate = f"  rate {user.per_minute_rate}/min" if user.per_minute_rate else ""
            out.write(f"  {user.username:<22} {user.role:<9} wallet {user.wallet.balance:>9}{rate}")
        out.write("\nRecharges")
        for recharge in RechargeTransaction.objects.filter(user__username__startswith=DEMO_PREFIX).order_by("id"):
            out.write(f"  {recharge.user.username:<22} {recharge.amount:>9}  {recharge.status:<8} {recharge.reference}")
        out.write("\nSessions")
        sessions = ConsultationSession.objects.filter(user__username__startswith=DEMO_PREFIX).order_by("id")
        for session in sessions.select_related("user", "provider"):
            provider = session.provider.username if session.provider else "-"
            out.write(
                f"  #{session.id:<4} {session.user.username:<18} {provider:<22} {session.status:<21} "
                f"billed {session.billed_minutes} min / {session.billed_amount:>7}  ended_by {session.ended_by or '-'}"
            )
