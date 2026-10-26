from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import ConsultationSession, RechargeTransaction, SessionBill, User, Wallet


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ("username", "email", "role", "per_minute_rate", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_active")
    fieldsets = UserAdmin.fieldsets + (("Billing role", {"fields": ("role", "per_minute_rate")}),)
    add_fieldsets = UserAdmin.add_fieldsets + (("Billing role", {"fields": ("role", "per_minute_rate")}),)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("user", "balance")
    search_fields = ("user__username",)


@admin.register(RechargeTransaction)
class RechargeTransactionAdmin(admin.ModelAdmin):
    list_display = ("reference", "user", "amount", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("reference", "user__username")
    readonly_fields = ("created_at",)


@admin.register(ConsultationSession)
class ConsultationSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "provider", "status", "per_minute_cost", "billed_amount", "ended_by")
    list_filter = ("status", "ended_by")
    search_fields = ("user__username", "provider__username")
    readonly_fields = ("created_at", "started_at", "ended_at", "billed_minutes", "billed_amount")


@admin.register(SessionBill)
class SessionBillAdmin(admin.ModelAdmin):
    list_display = ("session", "minute_number", "amount", "billed_at")
    search_fields = ("session__user__username", "session__provider__username")
    readonly_fields = ("billed_at",)
