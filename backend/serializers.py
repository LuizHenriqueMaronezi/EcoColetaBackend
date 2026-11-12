from rest_framework import serializers

from .models import Employee


# Dados que o app envia no login
class LoginSerializer(serializers.Serializer):
    login = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


# Dados do funcionário que o app recebe (somente leitura)
class EmployeeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Employee
        fields = ("id", "name", "login", "registration_number", "role")
        read_only_fields = fields