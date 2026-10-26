from rest_framework import serializers
from .models import ConsultationSession, RechargeTransaction, Wallet


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ("balance",)


class RechargeSerializer(serializers.ModelSerializer):
    class Meta:
        model = RechargeTransaction
        fields = ("id", "amount", "reference", "status", "created_at")
        read_only_fields = ("id", "reference", "status", "created_at")


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConsultationSession
        fields = ("id", "user", "provider", "status", "per_minute_cost", "locked_per_minute_cost", "started_at", "ended_at", "ended_by", "billed_minutes", "billed_amount", "actual_billable_minutes", "created_at")
        read_only_fields = ("user", "status", "locked_per_minute_cost", "started_at", "ended_at", "ended_by", "billed_minutes", "billed_amount", "actual_billable_minutes", "created_at")
