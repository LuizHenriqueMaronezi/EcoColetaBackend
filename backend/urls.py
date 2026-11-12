from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

# O router cria sozinho as rotas de lista (/bins/) e de detalhe (/bins/<id>/)
router = DefaultRouter()
router.register("bins", views.BinViewSet, basename="bin")
router.register("alerts", views.AlertViewSet, basename="alert")
router.register("collections", views.CollectionViewSet, basename="collection")

urlpatterns = [
    path("auth/login/", views.LoginView.as_view()),
    path("auth/logout/", views.LogoutView.as_view()),
    path("auth/me/", views.MeView.as_view()),
    path("", include(router.urls)),
]