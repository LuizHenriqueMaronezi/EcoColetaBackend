from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import ReadOnlyModelViewSet

from .models import Alert, Bin, Collection
from .serializers import (
    AlertSerializer,
    BinSerializer,
    CollectionSerializer,
    EmployeeSerializer,
    LoginSerializer,
)


# POST /api/auth/login/  ->  recebe login e senha, devolve o token e os dados do funcionário
class LoginView(APIView):
    authentication_classes = []  # o login não exige token
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        employee = authenticate(
            request,
            login=serializer.validated_data["login"],
            password=serializer.validated_data["password"],
        )
        # authenticate() devolve None se a senha estiver errada ou se o funcionário estiver inativo
        if employee is None:
            return Response(
                {"detail": "Login ou senha incorretos."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token, _ = Token.objects.get_or_create(user=employee)
        return Response({"token": token.key, "employee": EmployeeSerializer(employee).data})


# POST /api/auth/logout/  ->  apaga o token (o app precisa fazer login de novo)
class LogoutView(APIView):
    def post(self, request):
        request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# GET /api/auth/me/  ->  dados do funcionário dono do token
class MeView(APIView):
    def get(self, request):
        return Response(EmployeeSerializer(request.user).data)


# Lê um parâmetro numérico da URL (?bin=1). Se não for número, responde 400 em vez de dar erro 500.
def _int_param(request, name):
    value = request.query_params.get(name)
    if value is None or value == "":
        return None
    if not value.isdigit():
        raise ValidationError({name: "Informe um número inteiro."})
    return int(value)


# GET /api/bins/  e  GET /api/bins/<id>/
class BinViewSet(ReadOnlyModelViewSet):
    queryset = Bin.objects.order_by("code")
    serializer_class = BinSerializer
    pagination_class = None  # sem paginacao porque n tem tanta lixeira


# GET /api/alerts/  (só pendentes; ?include_resolved=true traz também os resolvidos; ?bin=<id> filtra)
class AlertViewSet(ReadOnlyModelViewSet):
    serializer_class = AlertSerializer
    pagination_class = None

    def get_queryset(self):
        queryset = Alert.objects.select_related("bin").order_by("-opened_at")
        if self.action == "list":  # os filtros valem só para a lista, não para um alerta específico
            if self.request.query_params.get("include_resolved") != "true":
                queryset = queryset.filter(resolved_at__isnull=True)
            bin_id = _int_param(self.request, "bin")
            if bin_id is not None:
                queryset = queryset.filter(bin_id=bin_id)
        return queryset


class CollectionPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# GET /api/collections/  (coletor vê só as próprias; supervisor vê todas)
# Filtros: ?bin=<id>  ?employee=<id>  ?status=in_progress|concluded|unconcluded  ?page=2
class CollectionViewSet(ReadOnlyModelViewSet):
    serializer_class = CollectionSerializer
    pagination_class = CollectionPagination

    def get_queryset(self):
        user = self.request.user
        queryset = Collection.objects.select_related("bin", "employee").order_by("-started_at")

        if user.role != "supervisor":
            queryset = queryset.filter(employee=user)  # coletor: só as próprias coletas

        if self.action == "list":
            bin_id = _int_param(self.request, "bin")
            if bin_id is not None:
                queryset = queryset.filter(bin_id=bin_id)

            employee_id = _int_param(self.request, "employee")
            if employee_id is not None:
                queryset = queryset.filter(employee_id=employee_id)

            wanted = self.request.query_params.get("status")
            if wanted == "in_progress":
                queryset = queryset.filter(finished_at__isnull=True)
            elif wanted in ("concluded", "unconcluded"):
                queryset = queryset.filter(result=wanted)
            elif wanted:
                raise ValidationError({"status": "Use in_progress, concluded ou unconcluded."})
        return queryset