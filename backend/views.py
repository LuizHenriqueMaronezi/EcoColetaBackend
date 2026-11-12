from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .serializers import EmployeeSerializer, LoginSerializer


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