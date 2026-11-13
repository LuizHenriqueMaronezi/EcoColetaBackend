import secrets  # geração da chave do Arduino
import uuid  # chaves UUID (ids gerados pelo app / Arduino)

# imports necessários para o usuário customizado
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import RegexValidator  

from django.db import models
from django.db.models import F, Q  # usados nas constraints

# Create your models here.

ROLES = [
    ("collector","Coletor"),
    ("supervisor","Supervisor")
]

BIN_STATE = [
    ("full", "Cheia"),
    ("notFull", "Não Cheia"),
    ("emptying", "Esvaziando")
]

COLLECTION_RESULTS = [
    ("concluded", "Bem Sucedida"),
    ("unconcluded", "Não Concluída")
]

ALERT_TYPES = [
    ("full_bin", "Lixeira Cheia"),
    ("collection_not_concluded", "Coleta Não Concluída")
]

# Equivale ao enum ReadingStatus do app Kotlin
READING_STATUS = [
    ("unknown", "Desconhecida"),
    ("valid", "Válida"),
    ("unavailable", "Indisponível")
]

# Equivale ao enum CollectionFailure do app Kotlin
COLLECTION_FAILURES = [
    ("time_unverified", "Tempo não verificado"),
    ("too_early", "Resultado enviado antes do prazo"),
    ("reading_unavailable", "Leitura indisponível"),
    ("bin_still_full", "Lixeira ainda cheia")
]

# Equivale ao enum TrackingCondition do app Kotlin
TRACKING_CONDITIONS = [
    ("awaiting_reading", "Aguardando leitura"),
    ("counting", "Contando"),
    ("permission_denied", "Permissão negada"),
    ("sensor_unavailable", "Sensor indisponível"),
    ("interrupted", "Interrompido")
]


# Manager (cria usuarios com senha em hash)
class EmployeeManager(BaseUserManager):
    use_in_migrations = True

    def get_by_natural_key(self, login):
        # login sem diferenciar maiusculas/minusculas
        return self.get(login=login.strip().lower())

    def create_user(self, login, password=None, **extra_fields):
        if not login:
            raise ValueError("O login é obrigatório.")
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        user = self.model(login=login.strip().lower(), **extra_fields)
        user.set_password(password)  # grava o hash e não a senha em si
        user.save(using=self._db)
        return user

    def create_superuser(self, login, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("role", "supervisor")
        return self.create_user(login, password, **extra_fields)


class Employee(AbstractBaseUser, PermissionsMixin):  
    name = models.CharField(max_length=120, null=False)
    login = models.CharField(max_length=120, null=False, unique=True)
    # [ALTERAÇÃO] adicionado validator: somente dígitos (até 5)
    registration_number = models.CharField(
        unique=True,
        max_length=5,
        validators=[RegexValidator(r"^[0-9]{5}$", "A matrícula deve ter exatamente 5 dígitos.")],
    )

    #  AbstractBaseUser já cria o campo "password" (128 chars) com hash

    role = models.CharField(max_length=120, choices=ROLES, default="collector")

    # [ADIÇÃO] AbstractBaseUser não cria is_active como campo; permite desativar em vez de apagar
    is_active = models.BooleanField(default=True)
    # [ADIÇÃO] necessário para acessar o Django admin (supervisores)
    is_staff = models.BooleanField(default=False)

    objects = EmployeeManager() 
    USERNAME_FIELD = "login" 
    REQUIRED_FIELDS = ["name", "registration_number"]

    # normaliza o login em qualquer gravação (inclusive pelo admin)
    def save(self, *args, **kwargs):
        self.login = self.login.strip().lower()
        super().save(*args, **kwargs)

    # nome legível no admin
    def __str__(self):
        return f"{self.name} ({self.login})"


def generate_bin_auth_key():
    # 16 caracteres hexadecimais: curta o bastante para a RAM do Arduino Uno
    return secrets.token_hex(8)


class Bin(models.Model):
    name = models.CharField(max_length=120, null=False)
    # Código da lixeira, gravado no firmware e usado no tópico MQTT (ecocoleta/lixeiras/<code>/...)
    code = models.SmallIntegerField(null=False, unique=True)


    percent_full = models.SmallIntegerField(null=True, blank=True)
    state = models.CharField(max_length=120, choices=BIN_STATE, null=True, blank=True)
    # chave que o Arduino envia em cada mensagem MQTT (texto simples, gerada automaticamente)
    auth_key = models.CharField(max_length=128, unique=True, default=generate_bin_auth_key)
    # [ALTERAÇÃO] mantido como você definiu. A garantia real de "uma coleta ativa por lixeira" está na
    # constraint de Collection (collection_one_in_progress_per_bin)
    active_collection = models.OneToOneField('Collection', on_delete=models.SET_NULL, null=True, blank=True, related_name='active_in_bin')

    # campos de leitura que o app Kotlin usa
    last_valid_at = models.DateTimeField(null=True, blank=True)
    last_reading_at = models.DateTimeField(null=True, blank=True)
    reading_status = models.CharField(max_length=20, choices=READING_STATUS, default="unknown")

    def __str__(self):
        return f"{self.name} (#{self.code})"

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(percent_full__isnull=True) | Q(percent_full__gte=0, percent_full__lte=100),
                name="bin_percent_full_0_100",
            ),
        ]


class Collection(models.Model):
    
    initial_percent = models.SmallIntegerField(null=True, blank=True) 
    final_percent = models.SmallIntegerField(null=True, blank=True)

    # Relação um para muitos
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, null=True, blank=True)
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT)

    result = models.CharField(max_length=120, choices=COLLECTION_RESULTS, null=True, blank=True)
    
    fail_reason = models.CharField(max_length=120, choices=COLLECTION_FAILURES, null=True, blank=True)

    started_at = models.DateTimeField()
    deadline_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    # [ADIÇÃO] ids dos eventos do Arduino (evitam coleta duplicada se o Arduino reenviar)
    start_event_id = models.UUIDField(unique=True)
    end_event_id = models.UUIDField(unique=True, null=True, blank=True)
    # [ADIÇÃO] leitura final como o Arduino reportou (o app Kotlin guarda ambas)
    reported_final_percent = models.SmallIntegerField(null=True, blank=True)
    reported_final_valid = models.BooleanField(null=True, blank=True)

    class Meta:
        constraints = [
            # Uma coleta em andamento por lixeira
            models.UniqueConstraint(
                fields=["bin"],
                condition=Q(finished_at__isnull=True),
                name="collection_one_in_progress_per_bin",
            ),
            models.CheckConstraint(
                condition=Q(initial_percent__isnull=True) | Q(initial_percent__gte=0, initial_percent__lte=100),
                name="collection_initial_percent_0_100",
            ),
            models.CheckConstraint(
                condition=Q(final_percent__isnull=True) | Q(final_percent__gte=0, final_percent__lte=100),
                name="collection_final_percent_0_100",
            ),
            models.CheckConstraint(
                condition=Q(deadline_at__gt=F("started_at")),
                name="collection_deadline_after_start",
            ),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True) | Q(finished_at__gte=F("started_at")),
                name="collection_finish_after_start",
            ),
            # result só existe quando a coleta terminou
            models.CheckConstraint(
                condition=(
                    Q(finished_at__isnull=True, result__isnull=True)
                    | Q(finished_at__isnull=False, result__isnull=False)
                ),
                name="collection_result_matches_finished",
            ),
            # fail_reason existe se, e somente se, result = "unconcluded"
            models.CheckConstraint(
                condition=(
                    Q(result="unconcluded", fail_reason__isnull=False)
                    | (~Q(result="unconcluded") & Q(fail_reason__isnull=True))
                ),
                name="collection_fail_reason_matches_result",
            ),
        ]
        indexes = [
            # "minhas coletas" e visão do supervisor
            models.Index(fields=["employee", "-started_at"], name="coll_employee_started_idx"),
        ]


class Alert(models.Model):
    type = models.CharField(max_length=120, choices=ALERT_TYPES)

    # Relação um pra muitos
    bin = models.ForeignKey(Bin, null=False, blank=False, on_delete=models.CASCADE)
    collection = models.ForeignKey(Collection, null=True, blank=True, on_delete=models.CASCADE)

    opened_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    # motivo da falha
    reason = models.CharField(max_length=120, choices=COLLECTION_FAILURES, null=True, blank=True)
    # evento do Arduino que abriu o alerta -> evita alerta duplicado
    source_event_id = models.UUIDField()

    class Meta:
        constraints = [
            # Um alerta de lixeira cheia pendente por lixeira
            models.UniqueConstraint(
                fields=["bin"],
                condition=Q(type="full_bin", resolved_at__isnull=True),
                name="alert_one_pending_full_per_bin",
            ),
            models.UniqueConstraint(
                fields=["source_event_id", "type"],
                name="alert_unique_source_event_type",
            ),
        ]


class Shift(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    started_at = models.DateTimeField(null=False)
    finished_at = models.DateTimeField(null=True, blank=True)

    # Relação um pra muitos
    # on delete protect -> para não sumir com o turno caso o funcionário seja apagado
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT)

    total_steps = models.IntegerField(default=0)
    is_partial_count = models.BooleanField(default=False)

    # choices com as 5 condições do app
    tracking_condition = models.CharField(max_length=100, choices=TRACKING_CONDITIONS, null=True, blank=True)

    class Meta:
        constraints = [
            # Um turno ativo por funcionário
            models.UniqueConstraint(
                fields=["employee"],
                condition=Q(finished_at__isnull=True),
                name="shift_one_active_per_employee",
            ),
            # turno começa antes e termina depois
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True) | Q(finished_at__gte=F("started_at")),
                name="shift_finish_after_start",
            ),
            # para que o turno não tenha numero de passos negativos
            models.CheckConstraint(
                condition=Q(total_steps__gte=0),
                name="shift_total_steps_not_negative",
            ),
        ]
        indexes = [
            models.Index(fields=["employee", "-started_at"], name="shift_employee_started_idx"),
        ]


# Histórico de medições do Arduino. O id vem do Arduino e serve para ignorar reenvios.
class MeasurementEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bin = models.ForeignKey(Bin, on_delete=models.PROTECT)
    measured_at = models.DateTimeField(null=True, blank=True)  # caso o Arduino informe o horario
    received_at = models.DateTimeField(auto_now_add=True)  # horário do servidor (o confiavel)
    reported_percent = models.SmallIntegerField(null=True, blank=True)
    reported_valid = models.BooleanField(default=False)

    class Meta:
        constraints = [
            # Porcentagem entre 0 e 100
            models.CheckConstraint(
                condition=Q(reported_percent__isnull=True) | Q(reported_percent__gte=0, reported_percent__lte=100),
                name="measurement_percent_0_100",
            ),
        ]
        indexes = [
            models.Index(fields=["bin", "-received_at"], name="measurement_bin_received_idx"),
        ]
