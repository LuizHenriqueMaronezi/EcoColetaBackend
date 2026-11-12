from rest_framework import serializers

from .models import Alert, Bin, Collection, Employee


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


# Estado da lixeira. O auth_key fica  da lista de propósito, já que nunca deve sair pela API.
class BinSerializer(serializers.ModelSerializer):
    class Meta:
        model = Bin
        fields = (
            "id", "name", "code", "percent_full", "state",
            "reading_status", "last_valid_at", "last_reading_at",
        )
        read_only_fields = fields


class AlertSerializer(serializers.ModelSerializer):
    bin_name = serializers.CharField(source="bin.name", read_only=True)

    class Meta:
        model = Alert
        fields = (
            "id", "type", "bin", "bin_name", "collection",
            "opened_at", "resolved_at", "reason",
        )
        read_only_fields = fields


class CollectionSerializer(serializers.ModelSerializer):
    bin_name = serializers.CharField(source="bin.name", read_only=True)
    # a coleta pode estar sem funcionário (ainda não houve o vínculo do QR)
    employee_name = serializers.CharField(source="employee.name", read_only=True, default=None)
    status = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = (
            "id", "bin", "bin_name", "employee", "employee_name", "status",
            "result", "fail_reason", "started_at", "deadline_at", "finished_at",
            "initial_percent", "final_percent",
        )
        read_only_fields = fields

    # in_progress enquanto não terminou -> depois  vira concluded ou unconcluded
    def get_status(self, obj):
        if obj.finished_at is None:
            return "in_progress"
        return obj.result