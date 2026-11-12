"""
API consumida pelo app Android. Contrato: frontend/docs/contratos/api-servidor.md.

O app atualiza por polling (a cada 30 s, ou ~10 s com coleta em andamento): ele sempre pergunta,
o servidor nunca chama o celular.
"""

import uuid
from datetime import timedelta

from django.contrib.auth import authenticate
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView

from .models import Alert, Bin, Collection, Shift
from .serializers import (
    AlertSerializer,
    BinSerializer,
    CollectionSerializer,
    EmployeeSerializer,
    LoginSerializer,
    ShiftSerializer,
    ShiftUploadSerializer,
)

# Quanto histórico de alertas resolvidos o app recebe (ele espelha a lista)
RESOLVED_ALERTS_WINDOW = timedelta(days=30)
MAX_ALERTS = 200
MAX_COLLECTIONS = 100


# GET /api/v1/  ->  índice navegável da API
class ApiRootView(APIView):
    def get(self, request, format=None):
        return Response({
            "me": reverse("me", request=request, format=format),
            "bins": reverse("bins", request=request, format=format),
            "alerts": reverse("alerts", request=request, format=format),
            "my_collections": reverse("my-collections", request=request, format=format),
        })


# POST /api/v1/auth/login/  ->  recebe login e senha, devolve o token e os dados do funcionário
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
                {"detail": "Identificação ou senha inválida."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token, _ = Token.objects.get_or_create(user=employee)
        return Response({"token": token.key, "employee": EmployeeSerializer(employee).data})


# POST /api/v1/auth/logout/  ->  apaga o token (o app precisa fazer login de novo)
class LogoutView(APIView):
    def post(self, request):
        if request.auth is not None:
            request.auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# GET /api/v1/me/  ->  dados do funcionário dono do token
class MeView(APIView):
    def get(self, request):
        return Response(EmployeeSerializer(request.user).data)


# GET /api/v1/bins/  ->  todas as lixeiras e a situação atual
class BinListView(APIView):
    def get(self, request):
        bins = Bin.objects.order_by("code")
        return Response(BinSerializer(bins, many=True).data)


# GET /api/v1/alerts/  ->  pendentes + resolvidos dos últimos 30 dias (no máximo 200)
class AlertListView(APIView):
    def get(self, request):
        since = timezone.now() - RESOLVED_ALERTS_WINDOW
        alerts = (
            Alert.objects.filter(Q(resolved_at__isnull=True) | Q(resolved_at__gte=since))
            .order_by("-opened_at")[:MAX_ALERTS]
        )
        return Response(AlertSerializer(alerts, many=True).data)


# GET /api/v1/me/collections/  ->  só as coletas do funcionário do token, mais recentes primeiro
class MyCollectionListView(APIView):
    def get(self, request):
        collections = Collection.objects.filter(employee=request.user).order_by("-started_at")[:MAX_COLLECTIONS]
        return Response(CollectionSerializer(collections, many=True).data)


# PUT /api/v1/me/shifts/<id>/  ->  envio idempotente do turno criado no aparelho
class MyShiftView(APIView):
    def put(self, request, shift_id):
        try:
            shift_uuid = uuid.UUID(str(shift_id))
        except ValueError:
            return Response({"id": "Identificador de turno inválido."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ShiftUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        body_id = serializer.validated_data.get("id")
        if body_id is not None and body_id != shift_uuid:
            return Response({"id": "O id do corpo difere do id da rota."}, status=status.HTTP_400_BAD_REQUEST)
        fields = serializer.to_model_fields()

        try:
            with transaction.atomic():
                shift = Shift.objects.select_for_update().filter(pk=shift_uuid).first()
                if shift is None:
                    shift = Shift.objects.create(id=shift_uuid, employee=request.user, **fields)
                    return Response(ShiftSerializer(shift).data, status=status.HTTP_201_CREATED)

                if shift.employee_id != request.user.pk:
                    return Response({"detail": "Turno de outro funcionário."}, status=status.HTTP_403_FORBIDDEN)
                if shift.finished_at is not None and fields["finished_at"] is None:
                    return Response({"detail": "Turno encerrado não pode ser reaberto."},
                                    status=status.HTTP_409_CONFLICT)
                if fields["total_steps"] < shift.total_steps:
                    return Response({"detail": "Os passos confirmados não podem diminuir."},
                                    status=status.HTTP_409_CONFLICT)

                for name, value in fields.items():
                    setattr(shift, name, value)
                shift.save()
        except IntegrityError:
            # por exemplo, outro turno ativo do mesmo funcionário
            return Response({"detail": "Já existe outro turno ativo para este funcionário."},
                            status=status.HTTP_409_CONFLICT)

        return Response(ShiftSerializer(shift).data, status=status.HTTP_200_OK)
