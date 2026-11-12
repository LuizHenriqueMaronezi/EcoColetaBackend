# Cria dados de exemplo para testar a API de leitura (1 lixeira, coletas e alertas).
# Uso, na raiz do projeto (mesma pasta do manage.py):  python seed_exemplo.py
import os
import uuid
from datetime import timedelta

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "EcoColeta.settings")
django.setup()

from django.utils import timezone

from backend.models import Alert, Bin, Collection, Employee

bin_ = Bin.objects.first()
collectors = list(Employee.objects.filter(role="collector", is_active=True).order_by("id")[:2])
if bin_ is None or not collectors:
    raise SystemExit("Cadastre antes, pelo admin, uma lixeira e pelo menos um funcionário coletor.")

now = timezone.now()
first, second = collectors[0], collectors[-1]

# Coleta bem-sucedida de ontem (funcionário 1)
start = now - timedelta(days=1)
Collection.objects.create(
    bin=bin_, employee=first, started_at=start, deadline_at=start + timedelta(seconds=180),
    finished_at=start + timedelta(seconds=190), initial_percent=95, final_percent=12,
    reported_final_percent=12, reported_final_valid=True, result="concluded",
    start_event_id=uuid.uuid4(), end_event_id=uuid.uuid4(),
)

# Coleta não concluída de hoje (funcionário 2) e o alerta dela
start = now - timedelta(hours=1)
failed = Collection.objects.create(
    bin=bin_, employee=second, started_at=start, deadline_at=start + timedelta(seconds=180),
    finished_at=start + timedelta(seconds=185), initial_percent=96, final_percent=91,
    reported_final_percent=91, reported_final_valid=True, result="unconcluded",
    fail_reason="bin_still_full", start_event_id=uuid.uuid4(), end_event_id=uuid.uuid4(),
)
Alert.objects.create(
    type="collection_not_concluded", bin=bin_, collection=failed,
    reason="bin_still_full", source_event_id=uuid.uuid4(),
)

# Se a lixeira ainda não tem leitura, deixa uma leitura de exemplo (cheia) para a tela do app
if bin_.percent_full is None:
    bin_.percent_full = 91
    bin_.state = "full"
    bin_.reading_status = "valid"
    bin_.last_valid_at = now
    bin_.last_reading_at = now
    bin_.save()
    if not Alert.objects.filter(bin=bin_, type="full_bin", resolved_at__isnull=True).exists():
        Alert.objects.create(type="full_bin", bin=bin_, source_event_id=uuid.uuid4())
