from django.urls import path

from . import views

# Rotas do contrato do app (frontend/docs/contratos/api-servidor.md), sob /api/v1/
urlpatterns = [
    path("", views.ApiRootView.as_view(), name="api-root"),
    path("auth/login/", views.LoginView.as_view(), name="login"),
    path("auth/logout/", views.LogoutView.as_view(), name="logout"),
    path("me/", views.MeView.as_view(), name="me"),
    path("me/collections/", views.MyCollectionListView.as_view(), name="my-collections"),
    path("me/shifts/<str:shift_id>/", views.MyShiftView.as_view(), name="my-shift"),
    path("bins/", views.BinListView.as_view(), name="bins"),
    path("alerts/", views.AlertListView.as_view(), name="alerts"),
]
