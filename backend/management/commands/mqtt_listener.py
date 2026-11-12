"""
Processo que escuta o Mosquitto e grava no banco o que os Arduinos publicam.

Uso (ao lado do runserver, em outro terminal):
    python manage.py mqtt_listener
"""

import logging
import threading
import time

import paho.mqtt.client as mqtt
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from backend import mqtt_protocol, services

log = logging.getLogger("ecocoleta.mqtt")

# A cada quantos segundos encerra coletas que nunca receberam o "fim".
EXPIRE_INTERVAL = 30


class Command(BaseCommand):
    help = "Escuta o broker MQTT (Mosquitto) e processa as mensagens das lixeiras."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="ecocoleta-backend",
            clean_session=True,
        )
        if settings.MQTT_USER:
            client.username_pw_set(settings.MQTT_USER, settings.MQTT_PASSWORD)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code.is_failure:
                log.error("Falha ao conectar no broker: %s", reason_code)
                return
            topic = mqtt_protocol.subscription_topic()
            client.subscribe(topic, qos=1)
            log.info("Conectado a %s:%s, escutando %s", settings.MQTT_HOST, settings.MQTT_PORT, topic)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            log.warning("Desconectado do broker (%s). Tentando reconectar...", reason_code)

        def on_message(client, userdata, message):
            close_old_connections()  # conexões ao Neon podem cair entre mensagens
            try:
                reply = mqtt_protocol.handle_message(message.topic, message.payload)
            except Exception:
                log.exception("Erro ao processar %s", message.topic)
                return
            if reply is not None:
                client.publish(reply.topic, reply.payload, qos=0)

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        stop = threading.Event()

        def expire_loop():
            while not stop.wait(EXPIRE_INTERVAL):
                close_old_connections()
                try:
                    expired = services.expire_stale_collections()
                    if expired:
                        log.info("%s coleta(s) sem fim encerrada(s) como não concluída(s)", expired)
                except Exception:
                    log.exception("Erro ao encerrar coletas expiradas")

        threading.Thread(target=expire_loop, daemon=True).start()

        log.info("Conectando ao broker %s:%s...", settings.MQTT_HOST, settings.MQTT_PORT)
        while True:
            try:
                client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
                break
            except OSError as exc:
                log.error("Broker indisponível (%s). Nova tentativa em 5 s.", exc)
                time.sleep(5)

        try:
            client.loop_forever(retry_first_connection=True)
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            client.disconnect()
