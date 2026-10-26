from django.urls import path
from .views import (ProviderSessionEndView, RechargeCallbackView, RechargeInitiateView,
                    SessionAcceptView, SessionAssignView, SessionBillView, SessionEndView,
                    SessionListCreateView, SessionStartView, WalletView)

urlpatterns = [
    path("wallet/", WalletView.as_view()),
    path("recharges/", RechargeInitiateView.as_view()),
    path("recharges/<str:reference>/callback/", RechargeCallbackView.as_view()),
    path("sessions/", SessionListCreateView.as_view()),
    path("sessions/<int:pk>/assign/", SessionAssignView.as_view()),
    path("sessions/<int:pk>/accept/", SessionAcceptView.as_view()),
    path("sessions/<int:pk>/start/", SessionStartView.as_view()),
    path("sessions/<int:pk>/bill/", SessionBillView.as_view()),
    path("sessions/<int:pk>/end/", SessionEndView.as_view()),
    path("sessions/<int:pk>/provider-end/", ProviderSessionEndView.as_view()),
]
