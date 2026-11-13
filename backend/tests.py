"""
Testes do backend. Rodam em SQLite (sem DATABASE_URL):  python manage.py test
"""

import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from . import mqtt_protocol, services
from .models import Alert, Bin, Collection, Employee, MeasurementEvent, Shift

PREFIX = "ecocoleta/lixeiras"


def up(code, kind):
    return f"{PREFIX}/{code}/up/{kind}"


class Fixture(TestCase):
    def setUp(self):
        self.bin = Bin.objects.create(name="Bloco A", code=1)
        self.ana = Employee.objects.create_user("ana.souza", "senha-forte-123", name="Ana Souza",
                                                registration_number="12345")
        self.bia = Employee.objects.create_user("bia", "senha-forte-123", name="Bia Lima",
                                                registration_number="54321")
        self.key = self.bin.auth_key
        self.now = timezone.now()

    def send(self, kind, payload, code=1):
        return mqtt_protocol.handle_message(up(code, kind), payload.encode())

    def measure(self, seq, percent, valid=1, boot="b1"):
        return self.send("medicao", f"k={self.key};b={boot};e={seq};p={percent};v={valid}")

    def start(self, seq, matricula="12345", percent=95, boot="b1"):
        return self.send("inicio", f"k={self.key};b={boot};e={seq};m={matricula};p={percent};v=1")

    def finish(self, seq, start_seq, percent, valid=1, elapsed=180, boot="b1"):
        return self.send("fim", f"k={self.key};b={boot};e={seq};s={start_seq};p={percent};v={valid};d={elapsed}")

    def later(self, seconds):
        """Simula o relógio do servidor `seconds` depois de agora."""
        return mock.patch("django.utils.timezone.now", return_value=self.now + timedelta(seconds=seconds))


# ---------------------------------------------------------------------------
# Protocolo
# ---------------------------------------------------------------------------

class ProtocolTests(Fixture):
    def test_parse_and_format(self):
        self.assertEqual(mqtt_protocol.parse_payload(b"k=abc;e=3;p=40;v=1"),
                         {"k": "abc", "e": "3", "p": "40", "v": "1"})
        self.assertEqual(mqtt_protocol.format_payload({"e": 3, "ok": 1, "n": "Ana"}), "e=3;ok=1;n=Ana")

    def test_wrong_key_is_ignored(self):
        self.assertIsNone(self.send("medicao", "k=errada;b=b1;e=1;p=50;v=1"))
        self.assertFalse(MeasurementEvent.objects.exists())

    def test_unknown_bin_is_ignored(self):
        self.assertIsNone(self.send("medicao", f"k={self.key};b=b1;e=1;p=50;v=1", code=99))

    def test_malformed_payload_is_ignored(self):
        self.assertIsNone(self.send("medicao", f"k={self.key};lixo"))
        self.assertIsNone(self.send("medicao", f"k={self.key};b=b1;p=50;v=1"))  # sem e
        self.assertIsNone(mqtt_protocol.handle_message("outro/topico", b"x=1"))

    def test_lcd_text_removes_accents_and_separators(self):
        self.assertEqual(services.lcd_text("João; Conceição=Araújo"), "Joao ConceicaoAr")


# ---------------------------------------------------------------------------
# Medições e alerta de cheia
# ---------------------------------------------------------------------------

class MeasurementTests(Fixture):
    def test_valid_reading_updates_bin(self):
        self.measure(1, 40)
        self.bin.refresh_from_db()
        self.assertEqual((self.bin.percent_full, self.bin.reading_status, self.bin.state), (40, "valid", "notFull"))

    def test_threshold_89_and_90(self):
        self.measure(1, 89)
        self.assertFalse(Alert.objects.exists())
        self.measure(2, 90)
        self.assertEqual(Alert.objects.filter(type="full_bin", resolved_at__isnull=True).count(), 1)
        self.bin.refresh_from_db()
        self.assertEqual(self.bin.state, "full")

    def test_single_pending_full_alert_and_resolution(self):
        self.measure(1, 95)
        self.measure(2, 100)
        self.assertEqual(Alert.objects.filter(type="full_bin").count(), 1)
        self.measure(3, 20)
        alert = Alert.objects.get(type="full_bin")
        self.assertIsNotNone(alert.resolved_at)

    def test_invalid_reading_keeps_last_valid_and_alert(self):
        self.measure(1, 95)
        self.measure(2, 0, valid=0)
        self.bin.refresh_from_db()
        self.assertEqual((self.bin.percent_full, self.bin.reading_status), (95, "unavailable"))
        self.assertTrue(Alert.objects.filter(type="full_bin", resolved_at__isnull=True).exists())

    def test_out_of_range_is_invalid(self):
        self.measure(1, 150)
        self.bin.refresh_from_db()
        self.assertEqual(self.bin.reading_status, "unavailable")

    def test_repeated_event_is_ignored(self):
        self.measure(1, 40)
        self.measure(1, 40)
        self.assertEqual(MeasurementEvent.objects.count(), 1)

    def test_same_seq_new_boot_is_new_event(self):
        self.measure(1, 40, boot="b1")
        self.measure(1, 41, boot="b2")
        self.assertEqual(MeasurementEvent.objects.count(), 2)


# ---------------------------------------------------------------------------
# Coletas
# ---------------------------------------------------------------------------

class CollectionTests(Fixture):
    def test_start_with_valid_registration(self):
        reply = self.start(1)
        self.assertEqual(reply.topic, f"{PREFIX}/1/down/resp")
        self.assertEqual(reply.payload, "e=1;ok=1;n=Ana Souza")
        collection = Collection.objects.get()
        self.assertEqual(collection.employee, self.ana)
        self.assertEqual(collection.initial_percent, 95)
        self.bin.refresh_from_db()
        self.assertEqual((self.bin.active_collection, self.bin.state), (collection, "emptying"))

    def test_start_with_unknown_or_inactive_registration(self):
        self.assertEqual(self.start(1, matricula="99999").payload, "e=1;ok=0;r=inv")
        self.bia.is_active = False
        self.bia.save()
        self.assertEqual(self.start(2, matricula="54321").payload, "e=2;ok=0;r=inv")
        self.assertFalse(Collection.objects.exists())

    def test_repeated_start_returns_same_answer(self):
        self.start(1)
        self.assertEqual(self.start(1).payload, "e=1;ok=1;n=Ana Souza")
        self.assertEqual(Collection.objects.count(), 1)

    def test_concurrent_start_is_busy(self):
        self.start(1)
        self.assertEqual(self.start(2, matricula="54321").payload, "e=2;ok=0;r=busy")

    def test_concluded_after_deadline(self):
        self.start(1)
        with self.later(181):
            reply = self.finish(2, 1, percent=10)
        self.assertEqual(reply.payload, "e=2;ok=1;c=1")
        collection = Collection.objects.get()
        self.assertEqual((collection.result, collection.fail_reason, collection.final_percent), ("concluded", None, 10))
        self.bin.refresh_from_db()
        self.assertIsNone(self.bin.active_collection)
        self.assertEqual((self.bin.state, self.bin.percent_full), ("notFull", 10))

    def test_bin_still_full(self):
        self.start(1)
        with self.later(181):
            reply = self.finish(2, 1, percent=92)
        self.assertEqual(reply.payload, "e=2;ok=1;c=0;r=bin_still_full")
        alert = Alert.objects.get(type="collection_not_concluded")
        self.assertEqual((alert.reason, alert.collection), ("bin_still_full", Collection.objects.get()))

    def test_too_early_by_server_clock(self):
        self.start(1)
        with self.later(60):
            reply = self.finish(2, 1, percent=10, elapsed=180)
        self.assertEqual(reply.payload, "e=2;ok=1;c=0;r=too_early")

    def test_too_early_by_device_clock(self):
        self.start(1)
        with self.later(181):
            reply = self.finish(2, 1, percent=10, elapsed=100)
        self.assertEqual(reply.payload, "e=2;ok=1;c=0;r=too_early")

    def test_time_unverified_when_clocks_disagree(self):
        self.start(1)
        with self.later(400):
            reply = self.finish(2, 1, percent=10, elapsed=180)
        self.assertEqual(reply.payload, "e=2;ok=1;c=0;r=time_unverified")

    def test_invalid_final_reading(self):
        self.start(1)
        with self.later(181):
            reply = self.finish(2, 1, percent=0, valid=0)
        self.assertEqual(reply.payload, "e=2;ok=1;c=0;r=reading_unavailable")

    def test_repeated_finish_returns_saved_result(self):
        self.start(1)
        with self.later(181):
            first = self.finish(2, 1, percent=10)
        with self.later(200):
            again = self.finish(2, 1, percent=10)
        self.assertEqual(first.payload, again.payload)
        self.assertEqual(Collection.objects.get().result, "concluded")

    def test_finish_without_collection(self):
        self.assertEqual(self.finish(2, 1, percent=10).payload, "e=2;ok=0;r=none")

    def test_concluded_resolves_previous_unconfirmed_alerts_and_full_alert(self):
        self.measure(1, 95)
        self.start(2)
        with self.later(181):
            self.finish(3, 2, percent=95)
        self.assertTrue(Alert.objects.filter(type="collection_not_concluded", resolved_at__isnull=True).exists())
        with self.later(200):
            self.start(4)
        with self.later(400):
            self.finish(5, 4, percent=5)
        self.assertFalse(Alert.objects.filter(resolved_at__isnull=True).exists())
        self.assertEqual(Alert.objects.count(), 2)  # histórico preservado

    def test_stale_collection_is_expired_on_new_start(self):
        self.start(1)
        with self.later(180 + 6 * 60):
            reply = self.start(1, boot="b2")  # Arduino reiniciou no meio da coleta
        self.assertEqual(reply.payload, "e=1;ok=1;n=Ana Souza")
        old, new = Collection.objects.order_by("started_at")
        self.assertEqual((old.result, old.fail_reason), ("unconcluded", "time_unverified"))
        self.assertIsNone(new.finished_at)

    def test_expire_stale_collections(self):
        self.start(1)
        with self.later(180 + 6 * 60):
            self.assertEqual(services.expire_stale_collections(), 1)
        self.assertEqual(Collection.objects.get().fail_reason, "time_unverified")
        self.bin.refresh_from_db()
        self.assertIsNone(self.bin.active_collection)

    @override_settings(COLLECTION_SECONDS=10)
    def test_short_collection_time_for_tests(self):
        self.start(1)
        with self.later(11):
            self.assertEqual(self.finish(2, 1, percent=10, elapsed=10).payload, "e=2;ok=1;c=1")


# ---------------------------------------------------------------------------
# API do app
# ---------------------------------------------------------------------------

class ApiTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.token = Token.objects.create(user=self.ana)
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def test_login_contract(self):
        anonymous = APIClient()
        response = anonymous.post("/api/v1/auth/login/", {"login": "Ana.Souza", "password": "senha-forte-123"},
                                  format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["employee"],
                         {"id": str(self.ana.pk), "display_name": "Ana Souza", "login": "ana.souza"})
        bad = anonymous.post("/api/v1/auth/login/", {"login": "ana.souza", "password": "x"}, format="json")
        self.assertEqual(bad.status_code, 401)

    def test_requires_token(self):
        self.assertEqual(APIClient().get("/api/v1/bins/").status_code, 401)

    def test_me(self):
        self.assertEqual(self.client.get("/api/v1/me/").json()["display_name"], "Ana Souza")

    def test_bins_contract(self):
        self.measure(1, 93)
        self.start(2)
        data = self.client.get("/api/v1/bins/").json()
        self.assertEqual(len(data), 1)
        item = data[0]
        self.assertEqual(set(item), {"id", "name", "occupancy_percent", "occupancy_measured_at",
                                     "last_reading_at", "reading_status", "active_collection_id"})
        self.assertEqual((item["id"], item["occupancy_percent"], item["reading_status"]), (str(self.bin.pk), 93, "valid"))
        self.assertEqual(item["active_collection_id"], str(Collection.objects.get().pk))
        self.assertNotIn("auth_key", item)

    def test_alerts_contract(self):
        self.measure(1, 95)
        self.start(2)
        with self.later(181):
            self.finish(3, 2, percent=95)
        types = {a["type"] for a in self.client.get("/api/v1/alerts/").json()}
        self.assertEqual(types, {"bin_full", "collection_unconfirmed"})
        unconfirmed = next(a for a in self.client.get("/api/v1/alerts/").json() if a["type"] == "collection_unconfirmed")
        self.assertEqual((unconfirmed["reason"], unconfirmed["bin_id"]), ("bin_still_full", str(self.bin.pk)))

    def test_old_resolved_alerts_are_hidden(self):
        alert = Alert.objects.create(type="full_bin", bin=self.bin, source_event_id=uuid.uuid4())
        alert.resolved_at = timezone.now() - timedelta(days=31)
        alert.save()
        self.assertEqual(self.client.get("/api/v1/alerts/").json(), [])

    def test_my_collections_only_own_and_status_mapping(self):
        self.start(1)  # Ana
        with self.later(181):
            self.finish(2, 1, percent=10)
        with self.later(200):
            self.start(3, matricula="54321")  # Bia
        data = self.client.get("/api/v1/me/collections/").json()
        self.assertEqual(len(data), 1)
        self.assertEqual((data[0]["status"], data[0]["failure_reason"], data[0]["final_percent"]), ("completed", None, 10))

    def test_shift_create_update_and_rules(self):
        shift_id = str(uuid.uuid4())
        url = f"/api/v1/me/shifts/{shift_id}/"
        body = {"id": shift_id, "started_at": "2026-11-11T11:00:00Z", "ended_at": None,
                "confirmed_steps": 100, "tracking_condition": "counting", "is_partial": False}
        self.assertEqual(self.client.put(url, body, format="json").status_code, 201)
        self.assertEqual(self.client.put(url, body, format="json").status_code, 200)  # idempotente

        body.update(confirmed_steps=50)
        self.assertEqual(self.client.put(url, body, format="json").status_code, 409)  # não reduz passos

        body.update(confirmed_steps=150, ended_at="2026-11-11T19:00:00Z")
        self.assertEqual(self.client.put(url, body, format="json").status_code, 200)
        shift = Shift.objects.get(pk=shift_id)
        self.assertEqual((shift.total_steps, shift.finished_at is not None), (150, True))

        body.update(ended_at=None)
        self.assertEqual(self.client.put(url, body, format="json").status_code, 409)  # não reabre

        other = APIClient()
        other.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.bia).key}")
        self.assertEqual(other.put(url, body, format="json").status_code, 403)

    def test_second_active_shift_conflicts(self):
        for expected in (201, 409):
            shift_id = str(uuid.uuid4())
            body = {"id": shift_id, "started_at": "2026-11-11T11:00:00Z", "confirmed_steps": 0,
                    "tracking_condition": "awaiting_reading"}
            self.assertEqual(self.client.put(f"/api/v1/me/shifts/{shift_id}/", body, format="json").status_code,
                             expected)

    def test_logout_revokes_token(self):
        self.assertEqual(self.client.post("/api/v1/auth/logout/").status_code, 204)
        self.assertEqual(self.client.get("/api/v1/me/").status_code, 401)
