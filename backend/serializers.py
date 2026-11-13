"""
Formato JSON definido pelo app Android (frontend/docs/contratos/api-servidor.md).
Os nomes internos dos modelos não mudam; os serializers traduzem para o contrato.
Ids saem como texto, porque o app trata identificadores como texto opaco.
"""

from rest_framework import serializers

from .models import TRACKING_CONDITIONS, Alert, Bin, Collection, Employee, Shift

# modelo -> contrato do app
ALERT_TYPE_OUT = {"full_bin": "bin_full", "collection_not_concluded": "collection_unconfirmed"}
COLLECTION_STATUS_OUT = {"concluded": "completed", "unconcluded": "unconfirmed"}


def _id(value):
    return None if value is None else str(value)


# Dados que o app envia no login
class LoginSerializer(serializers.Serializer):
    login = serializers.CharField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


# GET me/  e  "employee" do login
class EmployeeSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    display_name = serializers.CharField(source="name", read_only=True)

    class Meta:
        model = Employee
        fields = ("id", "display_name", "login")
        read_only_fields = fields

    def get_id(self, obj):
        return _id(obj.pk)


# GET bins/  (a auth_key nunca sai pela API)
class BinSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    occupancy_percent = serializers.IntegerField(source="percent_full", read_only=True)
    occupancy_measured_at = serializers.DateTimeField(source="last_valid_at", read_only=True)
    active_collection_id = serializers.SerializerMethodField()

    class Meta:
        model = Bin
        fields = (
            "id", "name", "occupancy_percent", "occupancy_measured_at",
            "last_reading_at", "reading_status", "active_collection_id",
        )
        read_only_fields = fields

    def get_id(self, obj):
        return _id(obj.pk)

    def get_active_collection_id(self, obj):
        return _id(obj.active_collection_id)


# GET alerts/
class AlertSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    bin_id = serializers.SerializerMethodField()
    type = serializers.SerializerMethodField()
    collection_id = serializers.SerializerMethodField()

    class Meta:
        model = Alert
        fields = ("id", "bin_id", "type", "collection_id", "reason", "opened_at", "resolved_at")
        read_only_fields = fields

    def get_id(self, obj):
        return _id(obj.pk)

    def get_bin_id(self, obj):
        return _id(obj.bin_id)

    def get_type(self, obj):
        return ALERT_TYPE_OUT.get(obj.type, obj.type)

    def get_collection_id(self, obj):
        return _id(obj.collection_id)


# GET me/collections/
class CollectionSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    bin_id = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    failure_reason = serializers.CharField(source="fail_reason", read_only=True)

    class Meta:
        model = Collection
        fields = (
            "id", "bin_id", "status", "started_at", "deadline_at", "finished_at",
            "initial_percent", "final_percent", "failure_reason",
        )
        read_only_fields = fields

    def get_id(self, obj):
        return _id(obj.pk)

    def get_bin_id(self, obj):
        return _id(obj.bin_id)

    def get_status(self, obj):
        if obj.finished_at is None:
            return "in_progress"
        return COLLECTION_STATUS_OUT.get(obj.result, obj.result)


# PUT me/shifts/{id}/  (o turno é criado no aparelho; nomes do app -> campos do modelo)
class ShiftUploadSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    started_at = serializers.DateTimeField()
    ended_at = serializers.DateTimeField(required=False, allow_null=True)
    confirmed_steps = serializers.IntegerField(min_value=0)
    tracking_condition = serializers.ChoiceField(choices=[code for code, _ in TRACKING_CONDITIONS])
    is_partial = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        ended = attrs.get("ended_at")
        if ended is not None and ended < attrs["started_at"]:
            raise serializers.ValidationError({"ended_at": "O fim do turno não pode ser antes do início."})
        return attrs

    def to_model_fields(self):
        data = self.validated_data
        return {
            "started_at": data["started_at"],
            "finished_at": data.get("ended_at"),
            "total_steps": data["confirmed_steps"],
            "tracking_condition": data["tracking_condition"],
            "is_partial_count": data.get("is_partial", False),
        }


class ShiftSerializer(serializers.ModelSerializer):
    id = serializers.SerializerMethodField()
    ended_at = serializers.DateTimeField(source="finished_at", read_only=True)
    confirmed_steps = serializers.IntegerField(source="total_steps", read_only=True)
    is_partial = serializers.BooleanField(source="is_partial_count", read_only=True)

    class Meta:
        model = Shift
        fields = ("id", "started_at", "ended_at", "confirmed_steps", "tracking_condition", "is_partial")
        read_only_fields = fields

    def get_id(self, obj):
        return _id(obj.pk)
