from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from .models import Alert, Bin, BinClaim, Collection, Employee, MeasurementEvent, Shift


# Formulário de criação de funcionário: pede a senha duas vezes e grava com hash
class EmployeeCreationForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = Employee
        fields = ("login", "name", "registration_number", "role")
        field_classes = {}


# Formulário de edição de funcionário: a senha aparece só como hash, com link para trocá-la
class EmployeeChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = Employee
        fields = "__all__"
        field_classes = {}


@admin.register(Employee)
class EmployeeAdmin(BaseUserAdmin):
    form = EmployeeChangeForm
    add_form = EmployeeCreationForm

    list_display = ("name", "login", "registration_number", "role", "is_active")
    list_filter = ("role", "is_active")
    search_fields = ("name", "login", "registration_number")
    ordering = ("name",)

    fieldsets = (
        (None, {"fields": ("login", "password")}),
        ("Dados do funcionário", {"fields": ("name", "registration_number", "role")}),
        ("Permissões", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Datas", {"fields": ("last_login",)}),
    )
    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("login", "name", "registration_number", "role", "password1", "password2"),
        }),
    )
    filter_horizontal = ("groups", "user_permissions")


@admin.register(Bin)
class BinAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "state", "percent_full", "reading_status", "last_reading_at")
    search_fields = ("name",)


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ("id", "bin", "employee", "started_at", "finished_at", "result", "fail_reason")
    list_filter = ("result", "bin")
    date_hierarchy = "started_at"


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "bin", "opened_at", "resolved_at", "reason")
    list_filter = ("type", "bin")


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ("employee", "started_at", "finished_at", "total_steps", "is_partial_count")
    list_filter = ("employee",)
    date_hierarchy = "started_at"


@admin.register(MeasurementEvent)
class MeasurementEventAdmin(admin.ModelAdmin):
    list_display = ("bin", "received_at", "reported_percent", "reported_valid")
    list_filter = ("bin", "reported_valid")


@admin.register(BinClaim)
class BinClaimAdmin(admin.ModelAdmin):
    list_display = ("bin", "employee", "created_at", "expires_at", "released_at")