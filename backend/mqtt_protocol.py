"""
Protocolo MQTT entre o Arduino (Uno + W5100) e o backend.

Payload compacto em texto (o Uno tem 2 KB de RAM): pares chave=valor separados por ';'.

Tópicos ({prefixo} = ecocoleta/lixeiras, {code} = Bin.code):

  {prefixo}/{code}/up/medicao   Arduino -> backend   k=<chave>;b=<boot>;e=<seq>;p=<0-100>;v=<1|0>
  {prefixo}/{code}/up/inicio    Arduino -> backend   k=<chave>;b=<boot>;e=<seq>;m=<matricula>;p=<0-100>;v=<1|0>
  {prefixo}/{code}/up/fim       Arduino -> backend   k=<chave>;b=<boot>;e=<seq>;s=<seq do inicio>;p=<0-100>;v=<1|0>;d=<segundos>
  {prefixo}/{code}/down/resp    backend -> Arduino   respostas abaixo

Respostas (sempre com e=<seq> do evento respondido):
  inicio ok      e=<seq>;ok=1;n=<nome para o LCD>
  inicio recusa  e=<seq>;ok=0;r=inv|busy           (matrícula inválida | coleta em andamento)
  fim            e=<seq>;ok=1;c=1                   (concluída)
                 e=<seq>;ok=1;c=0;r=<motivo>        (não concluída: bin_still_full, too_early, ...)
                 e=<seq>;ok=0;r=none                (não há coleta em andamento para esse início)

Campos:
  k  chave da lixeira (Bin.auth_key)          b  id aleatório gerado a cada boot do Arduino
  e  sequência do evento dentro do boot       s  sequência do evento de início desta coleta
  p  ocupação em %                            v  1 = leitura válida, 0 = falha do sensor
  m  matrícula (5 dígitos)                    d  segundos de espera medidos pelo Arduino
"""

import logging
from dataclasses import dataclass

from django.conf import settings

from . import services

log = logging.getLogger("ecocoleta.mqtt")


class ProtocolError(ValueError):
    pass


def parse_payload(payload):
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("ascii", errors="replace")
    fields = {}
    for part in payload.strip().split(";"):
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep or not key:
            raise ProtocolError(f"campo malformado: {part!r}")
        fields[key.strip()] = value.strip()
    return fields


def format_payload(fields):
    return ";".join(f"{key}={value}" for key, value in fields.items())


def parse_topic(topic):
    """Retorna (code, tipo) para '{prefixo}/{code}/up/{tipo}'."""
    prefix = settings.MQTT_TOPIC_PREFIX.rstrip("/") + "/"
    if not topic.startswith(prefix):
        raise ProtocolError(f"tópico fora do prefixo: {topic}")
    parts = topic[len(prefix):].split("/")
    if len(parts) != 3 or parts[1] != "up":
        raise ProtocolError(f"tópico inesperado: {topic}")
    return parts[0], parts[2]


def response_topic(code):
    return f"{settings.MQTT_TOPIC_PREFIX.rstrip('/')}/{code}/down/resp"


def subscription_topic():
    return f"{settings.MQTT_TOPIC_PREFIX.rstrip('/')}/+/up/+"


def _int(fields, key, required=True):
    value = fields.get(key)
    if value is None or value == "":
        if required:
            raise ProtocolError(f"campo {key} ausente")
        return None
    try:
        return int(value)
    except ValueError:
        raise ProtocolError(f"campo {key} não numérico: {value!r}")


def _reading(fields):
    valid = fields.get("v") == "1"
    percent = _int(fields, "p", required=False)
    if percent is not None and not 0 <= percent <= 100:
        valid = False
    return percent, valid


@dataclass
class Reply:
    topic: str
    payload: str


def handle_message(topic, payload):
    """
    Processa uma mensagem recebida do Arduino. Retorna a resposta a publicar (ou None).
    Mensagens de lixeira desconhecida, chave errada ou malformadas são descartadas com log.
    """
    try:
        code, kind = parse_topic(topic)
        fields = parse_payload(payload)
        bin_ = services.authenticate_bin(code, fields.get("k"))
        boot = fields.get("b") or ""
        seq = _int(fields, "e")
        if not boot:
            raise ProtocolError("campo b ausente")
    except (ProtocolError, services.DeviceAuthError) as exc:
        log.warning("Mensagem descartada (%s): %s", topic, exc)
        return None

    event_id = services.event_uuid(bin_.code, boot, seq)
    reply = {"e": seq}

    try:
        if kind == "medicao":
            percent, valid = _reading(fields)
            services.record_measurement(bin_, event_id, percent, valid)
            log.info("Lixeira %s: medição %s%% (%s)", code, percent, "válida" if valid else "inválida")
            return None  # medição não tem resposta

        if kind == "inicio":
            percent, valid = _reading(fields)
            result = services.start_collection(bin_, event_id, fields.get("m"), percent, valid)
            if result.ok:
                reply.update(ok=1, n=result.employee_name)
                log.info("Lixeira %s: coleta iniciada por %s", code, result.employee_name)
            else:
                reply.update(ok=0, r=result.error)
                log.info("Lixeira %s: início recusado (%s)", code, result.error)
            return Reply(response_topic(code), format_payload(reply))

        if kind == "fim":
            percent, valid = _reading(fields)
            start_id = services.event_uuid(bin_.code, boot, _int(fields, "s"))
            result = services.finish_collection(bin_, event_id, start_id, percent, valid, _int(fields, "d", False))
            if not result.ok:
                reply.update(ok=0, r=result.error)
            elif result.concluded:
                reply.update(ok=1, c=1)
            else:
                reply.update(ok=1, c=0, r=result.reason)
            log.info("Lixeira %s: fim da coleta -> %s", code, format_payload(reply))
            return Reply(response_topic(code), format_payload(reply))
    except ProtocolError as exc:
        log.warning("Mensagem descartada (%s): %s", topic, exc)
        return None

    log.warning("Tipo de mensagem desconhecido: %s", kind)
    return None
