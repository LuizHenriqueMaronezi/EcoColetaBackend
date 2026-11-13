"""
Regras de negócio das lixeiras, coletas e alertas.

O Arduino só reporta fatos (medições, início e fim da coleta). Quem decide o resultado da coleta
é o servidor, usando o próprio relógio (o Arduino não tem relógio real). Cada evento do Arduino tem
um id estável: reenviar o mesmo evento devolve o resultado já salvo, sem duplicar nada.

Regras (docs/contratos/coleta-e-alertas.md do app):
- leitura válida >= FULL_THRESHOLD (90%) = cheia; abaixo = disponível;
- a coleta espera COLLECTION_SECONDS (180 s) e só é concluída com leitura final válida abaixo de 90%
  depois do prazo;
- uma coleta em andamento por lixeira e um alerta de cheia pendente por lixeira;
- coleta concluída resolve os alertas de coleta não concluída anteriores da mesma lixeira.
"""

import hmac
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import Alert, Bin, Collection, Employee, MeasurementEvent

# Namespace fixo para transformar (lixeira, boot, sequência) do Arduino em UUID determinístico.
EVENT_NAMESPACE = uuid.UUID("6f1d2a4e-5c3b-4e8a-9b7d-0e2f4a6c8b1d")

# Tolerância entre o tempo medido pelo Arduino e o tempo visto pelo servidor.
CLOCK_TOLERANCE = timedelta(seconds=5)
# Diferença máxima aceitável entre o tempo de espera informado pelo Arduino e o visto pelo servidor.
MAX_ELAPSED_DRIFT = timedelta(seconds=60)
# Depois do prazo, quanto tempo a coleta pode ficar sem "fim" antes de ser encerrada pelo servidor.
STALE_GRACE = timedelta(minutes=5)


class DeviceAuthError(Exception):
    """Lixeira desconhecida ou chave errada: a mensagem é ignorada."""


def event_uuid(bin_code, boot_id, seq):
    return uuid.uuid5(EVENT_NAMESPACE, f"{bin_code}:{boot_id}:{seq}")


def authenticate_bin(code, auth_key):
    try:
        bin_ = Bin.objects.get(code=int(code))
    except (Bin.DoesNotExist, ValueError, TypeError):
        raise DeviceAuthError(f"lixeira {code!r} não cadastrada")
    if not hmac.compare_digest(str(bin_.auth_key), str(auth_key or "")):
        raise DeviceAuthError(f"chave inválida para a lixeira {code}")
    return bin_


def lcd_text(value, size=16):
    """Texto para o LCD 16x2: sem acentos, só ASCII imprimível, sem ';' e '=' (separadores)."""
    ascii_text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    cleaned = "".join(ch for ch in ascii_text if 32 <= ord(ch) < 127 and ch not in ";=")
    return cleaned.strip()[:size]


def _is_full(percent):
    return percent >= settings.FULL_THRESHOLD


# ---------------------------------------------------------------------------
# Medições
# ---------------------------------------------------------------------------

def _apply_reading(bin_, event_id, percent, valid, now):
    """Grava a medição, atualiza a lixeira e abre/resolve o alerta de cheia. Chamar dentro de transação."""
    valid = bool(valid) and percent is not None and 0 <= percent <= 100
    MeasurementEvent.objects.create(
        id=event_id, bin=bin_, reported_percent=percent if valid else None, reported_valid=valid,
    )
    bin_.last_reading_at = now
    if valid:
        bin_.percent_full = percent
        bin_.last_valid_at = now
        bin_.reading_status = "valid"
        if bin_.active_collection_id is None:
            bin_.state = "full" if _is_full(percent) else "notFull"
    else:
        # leitura falhou: preserva o último percentual válido
        bin_.reading_status = "unavailable"
    bin_.save()

    if not valid:
        return  # leitura inválida não abre nem resolve alerta de cheia
    pending_full = Alert.objects.filter(bin=bin_, type="full_bin", resolved_at__isnull=True)
    if _is_full(percent):
        if not pending_full.exists():
            Alert.objects.create(type="full_bin", bin=bin_, source_event_id=event_id)
    else:
        pending_full.update(resolved_at=now)


def record_measurement(bin_, event_id, percent, valid):
    """Medição periódica do Arduino. Repetida (mesmo id) é ignorada."""
    with transaction.atomic():
        bin_ = Bin.objects.select_for_update().get(pk=bin_.pk)
        if MeasurementEvent.objects.filter(id=event_id).exists():
            return False
        _apply_reading(bin_, event_id, percent, valid, timezone.now())
        return True


# ---------------------------------------------------------------------------
# Coletas
# ---------------------------------------------------------------------------

@dataclass
class StartResult:
    ok: bool
    employee_name: str = ""
    error: str = ""  # "inv" matrícula inválida | "busy" já existe coleta em andamento
    collection: Collection | None = None


@dataclass
class FinishResult:
    ok: bool
    concluded: bool = False
    reason: str = ""  # código de COLLECTION_FAILURES quando não concluída
    error: str = ""  # "none" sem coleta em andamento
    collection: Collection | None = None


def _finish(collection, now, result, reason, end_event_id=None, final_percent=None, final_valid=None):
    """Encerra a coleta, libera a lixeira e atualiza alertas. Chamar dentro de transação."""
    collection.finished_at = max(now, collection.started_at)
    collection.result = result
    collection.fail_reason = reason if result == "unconcluded" else None
    collection.end_event_id = end_event_id
    collection.reported_final_percent = final_percent
    collection.reported_final_valid = final_valid
    collection.final_percent = final_percent if final_valid else None
    collection.save()

    bin_ = collection.bin
    bin_.active_collection = None
    if bin_.percent_full is not None:
        bin_.state = "full" if _is_full(bin_.percent_full) else "notFull"
    else:
        bin_.state = None
    bin_.save()

    if result == "concluded":
        Alert.objects.filter(
            bin=bin_, type="collection_not_concluded", resolved_at__isnull=True,
        ).update(resolved_at=now)
    else:
        Alert.objects.create(
            type="collection_not_concluded", bin=bin_, collection=collection, reason=reason,
            source_event_id=end_event_id or uuid.uuid4(),
        )


def _expire_if_stale(collection, now):
    """Coleta cujo fim nunca chegou (Arduino reiniciou, perdeu a rede...): não dá para verificar o tempo."""
    if collection.finished_at is None and now > collection.deadline_at + STALE_GRACE:
        _finish(collection, now, "unconcluded", "time_unverified")
        return True
    return False


def expire_stale_collections():
    """Executado periodicamente pelo listener MQTT."""
    now = timezone.now()
    expired = 0
    stale = Collection.objects.filter(finished_at__isnull=True, deadline_at__lt=now - STALE_GRACE)
    for collection_id in stale.values_list("id", flat=True):
        with transaction.atomic():
            collection = Collection.objects.select_for_update().select_related("bin").get(pk=collection_id)
            if _expire_if_stale(collection, now):
                expired += 1
    return expired


def start_collection(bin_, event_id, registration_number, percent=None, valid=False):
    """
    Teclado: matrícula + senha correta. Valida a matrícula e abre a coleta numa única etapa.
    Repetir o mesmo evento devolve a mesma resposta.
    """
    existing = Collection.objects.select_related("employee").filter(start_event_id=event_id).first()
    if existing is not None:
        return StartResult(ok=True, employee_name=lcd_text(existing.employee.name if existing.employee else ""),
                           collection=existing)

    employee = Employee.objects.filter(
        registration_number=str(registration_number or "").strip(), is_active=True,
    ).first()
    if employee is None:
        return StartResult(ok=False, error="inv")

    now = timezone.now()
    try:
        with transaction.atomic():
            bin_ = Bin.objects.select_for_update().get(pk=bin_.pk)
            current = Collection.objects.select_for_update().filter(bin=bin_, finished_at__isnull=True).first()
            if current is not None and not _expire_if_stale(current, now):
                return StartResult(ok=False, error="busy")
            if current is not None:
                bin_.refresh_from_db()

            valid = bool(valid) and percent is not None and 0 <= percent <= 100
            collection = Collection.objects.create(
                bin=bin_, employee=employee, started_at=now,
                deadline_at=now + timedelta(seconds=settings.COLLECTION_SECONDS),
                initial_percent=percent if valid else bin_.percent_full,
                start_event_id=event_id,
            )
            bin_.active_collection = collection
            bin_.state = "emptying"
            bin_.save()
    except IntegrityError:
        # corrida com outro início para a mesma lixeira ou o mesmo evento
        again = Collection.objects.filter(start_event_id=event_id).select_related("employee").first()
        if again is not None:
            return StartResult(ok=True, employee_name=lcd_text(employee.name), collection=again)
        return StartResult(ok=False, error="busy")

    return StartResult(ok=True, employee_name=lcd_text(employee.name), collection=collection)


def finish_collection(bin_, event_id, start_event_id, percent, valid, elapsed_seconds):
    """
    Fim da espera no Arduino, com a leitura final. O servidor decide:
    - leitura inválida                         -> não concluída (reading_unavailable)
    - antes do prazo (relógio do servidor)     -> não concluída (too_early)
    - tempo do Arduino incoerente com o nosso  -> não concluída (time_unverified)
    - leitura válida >= 90%                    -> não concluída (bin_still_full)
    - leitura válida < 90% depois do prazo     -> concluída
    """
    done = Collection.objects.filter(end_event_id=event_id).first()
    if done is not None:
        return FinishResult(ok=True, concluded=done.result == "concluded", reason=done.fail_reason or "",
                            collection=done)

    now = timezone.now()
    with transaction.atomic():
        bin_ = Bin.objects.select_for_update().get(pk=bin_.pk)
        collection = (
            Collection.objects.select_for_update().select_related("bin")
            .filter(bin=bin_, start_event_id=start_event_id, finished_at__isnull=True).first()
        )
        if collection is None:
            return FinishResult(ok=False, error="none")

        valid = bool(valid) and percent is not None and 0 <= percent <= 100
        # a leitura final também vale como medição da lixeira
        _apply_reading(bin_, event_id, percent if valid else None, valid, now)
        collection.bin.refresh_from_db()

        server_elapsed = now - collection.started_at
        device_elapsed = timedelta(seconds=elapsed_seconds) if elapsed_seconds is not None else None

        if not valid:
            result, reason = "unconcluded", "reading_unavailable"
        elif now + CLOCK_TOLERANCE < collection.deadline_at or (
            device_elapsed is not None and device_elapsed + CLOCK_TOLERANCE < timedelta(seconds=settings.COLLECTION_SECONDS)
        ):
            result, reason = "unconcluded", "too_early"
        elif device_elapsed is None or abs(server_elapsed - device_elapsed) > MAX_ELAPSED_DRIFT:
            result, reason = "unconcluded", "time_unverified"
        elif _is_full(percent):
            result, reason = "unconcluded", "bin_still_full"
        else:
            result, reason = "concluded", None

        _finish(collection, now, result, reason, end_event_id=event_id,
                final_percent=percent if valid else None, final_valid=valid)

    return FinishResult(ok=True, concluded=result == "concluded", reason=reason or "", collection=collection)
