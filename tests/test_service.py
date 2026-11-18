import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.service import Service
from app.store import SeenStore
from app.web import authorized, create_app


class FakeAvito:
    def __init__(self, chats=None, chat=None):
        self.chats = chats or []
        self.chat = chat or {}
        self.subscribed = []

    async def get_self(self):
        return {"id": 10, "name": "Продавец"}

    async def get_chat(self, user_id, chat_id):
        return self.chat

    async def unread_chats(self, user_id):
        return {"chats": self.chats}

    async def subscribe(self, url):
        self.subscribed.append(url)
        return {"ok": True}


class FakeTelegram:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    async def send(self, text):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        self.sent.append(text)


def _settings(tmp: Path) -> Settings:
    return Settings(
        avito_client_id="id",
        avito_client_secret="secret",
        avito_user_id=10,
        telegram_bot_token="token",
        telegram_chat_id="1",
        webhook_secret="s3cret",
        state_path=str(tmp / "state.json"),
        subscribe_on_start=False,
    )


def _buyer_event(message_id="m-1", author_id=20, text="Здравствуйте"):
    return {
        "payload": {
            "type": "message",
            "value": {
                "id": message_id,
                "chat_id": "chat-1",
                "user_id": 10,
                "author_id": author_id,
                "type": "text",
                "content": {"text": text},
                "created": 1_700_000_000,
            },
        }
    }


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_sends_buyer_message_once(self):
        with tempfile.TemporaryDirectory() as folder:
            service = self._service(Path(folder))
            await service.handle_webhook(_buyer_event())
            await service.handle_webhook(_buyer_event())
            self.assertEqual(len(service.telegram.sent), 1)
            self.assertIn("Здравствуйте", service.telegram.sent[0])
            self.assertIn("Диван", service.telegram.sent[0])

    async def test_own_message_is_not_sent(self):
        with tempfile.TemporaryDirectory() as folder:
            service = self._service(Path(folder))
            await service.handle_webhook(_buyer_event(author_id=10, text="Я сам"))
            self.assertEqual(service.telegram.sent, [])

    async def test_failed_send_can_be_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            service = self._service(Path(folder), fail_times=1)
            await service.handle_webhook(_buyer_event())
            await service.handle_webhook(_buyer_event())
            self.assertEqual(len(service.telegram.sent), 1)

    async def test_first_poll_keeps_old_unread_and_sends_fresh(self):
        with tempfile.TemporaryDirectory() as folder:
            chats = [
                {
                    "id": "old-chat",
                    "last_message": {
                        "id": "old",
                        "author_id": 20,
                        "direction": "in",
                        "type": "text",
                        "content": {"text": "старое"},
                        "created": 1_000,
                    },
                },
                {
                    "id": "new-chat",
                    "last_message": {
                        "id": "new",
                        "author_id": 20,
                        "direction": "in",
                        "type": "text",
                        "content": {"text": "свежее"},
                        "created": service_now(),
                    },
                },
            ]
            service = self._service(Path(folder), chats=chats)
            await service.poll_once(seed_only=True)
            texts = " ".join(service.telegram.sent)
            self.assertIn("свежее", texts)
            self.assertNotIn("старое", texts)

    def _service(self, folder: Path, fail_times=0, chats=None) -> Service:
        settings = _settings(folder)
        service = Service(
            settings,
            avito=FakeAvito(
                chats=chats,
                chat={
                    "context": {
                        "value": {
                            "title": "Диван",
                            "url": "https://www.avito.ru/divan",
                        }
                    },
                    "users": [{"id": 20, "name": "Анна"}],
                },
            ),
            telegram=FakeTelegram(fail_times=fail_times),
            store=SeenStore(folder / "state.json"),
        )
        return service


def service_now() -> int:
    import time

    return int(time.time())


class WebTests(unittest.TestCase):
    def test_webhook_requires_secret(self):
        stub_events = []

        class Stub:
            async def handle_webhook(self, body):
                stub_events.append(body)

        with tempfile.TemporaryDirectory() as folder:
            app = create_app(_settings(Path(folder)), Stub())  # type: ignore[arg-type]
            with TestClient(app) as client:
                denied = client.post("/avito/webhook", json={"payload": {}})
                allowed = client.post(
                    "/avito/webhook?token=s3cret",
                    json={"payload": {"type": "message"}},
                )
                self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(stub_events, [{"payload": {"type": "message"}}])

    def test_empty_secret_rejects_everyone(self):
        self.assertFalse(authorized("", ""))
        self.assertFalse(authorized("s3cret", ""))
        self.assertTrue(authorized("s3cret", "s3cret"))


class StoreTests(unittest.TestCase):
    def test_claim_is_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            store = SeenStore(path)
            self.assertTrue(store.claim("a"))
            self.assertFalse(store.claim("a"))
            again = SeenStore(path)
            self.assertFalse(again.claim("a"))
            again.release("a")
            self.assertTrue(SeenStore(path).claim("a"))


class SettingsTests(unittest.TestCase):
    def test_webhook_url_quotes_secret(self):
        settings = Settings(
            public_base_url="https://example.test",
            webhook_secret="abc",
        )
        self.assertEqual(
            settings.public_webhook_url(),
            "https://example.test/avito/webhook?token=abc",
        )


if __name__ == "__main__":
    unittest.main()
