import unittest
from datetime import datetime, timezone

from app.messages import (
    format_created,
    format_notification,
    is_historical,
    is_outgoing,
    message_from_chat,
    parse_chat_list,
    parse_webhook,
)


def _event(**value):
    return {"id": "wh-1", "payload": {"type": "message", "value": value}}


class ParseWebhookTests(unittest.TestCase):
    def test_text_message(self):
        message = parse_webhook(
            _event(
                id="m-1",
                chat_id="u2i-chat",
                user_id=10,
                author_id=20,
                item_id=55,
                type="text",
                created=1_700_000_000,
                content={"text": "  Ещё продаёте? "},
            )
        )
        assert message is not None
        self.assertEqual(message.message_id, "m-1")
        self.assertEqual(message.chat_id, "u2i-chat")
        self.assertEqual(message.author_id, 20)
        self.assertEqual(message.account_user_id, 10)
        self.assertEqual(message.item_id, 55)
        self.assertEqual(message.text, "Ещё продаёте?")

    def test_image_without_text(self):
        message = parse_webhook(
            _event(id="m-2", chat_id="c", author_id=2, type="image", content={})
        )
        assert message is not None
        self.assertEqual(message.text, "[изображение]")

    def test_read_event_is_ignored(self):
        self.assertIsNone(
            parse_webhook(
                {
                    "payload": {
                        "type": "read",
                        "value": {"id": "m", "chat_id": "c"},
                    }
                }
            )
        )

    def test_missing_chat_is_ignored(self):
        self.assertIsNone(parse_webhook(_event(id="m", content={"text": "x"})))

    def test_outgoing_by_direction_and_author(self):
        incoming = parse_webhook(
            _event(id="1", chat_id="c", author_id=10, user_id=10, direction="in")
        )
        outgoing = parse_webhook(
            _event(id="2", chat_id="c", author_id=10, user_id=10, direction="out")
        )
        own = parse_webhook(_event(id="3", chat_id="c", author_id=10, user_id=10))
        buyer = parse_webhook(_event(id="4", chat_id="c", author_id=20, user_id=10))
        assert incoming and outgoing and own and buyer
        self.assertFalse(is_outgoing(incoming, 10, notify_own=False))
        self.assertTrue(is_outgoing(outgoing, 10, notify_own=False))
        self.assertTrue(is_outgoing(own, None, notify_own=False))
        self.assertFalse(is_outgoing(buyer, 10, notify_own=False))
        self.assertFalse(is_outgoing(own, 10, notify_own=True))

    def test_chat_list_uses_last_message(self):
        payload = {
            "chats": [
                {
                    "id": "chat-9",
                    "context": {"value": {"id": 77, "title": "Стол"}},
                    "last_message": {
                        "id": "m-9",
                        "author_id": 3,
                        "direction": "in",
                        "type": "text",
                        "content": {"text": "Беру"},
                        "created": 1_700_000_000,
                    },
                }
            ]
        }
        found = parse_chat_list(payload)
        self.assertEqual(len(found), 1)
        message, chat = found[0]
        self.assertEqual(message.chat_id, "chat-9")
        self.assertEqual(message.item_id, 77)
        self.assertEqual(message.text, "Беру")
        self.assertEqual(message_from_chat(chat).message_id, "m-9")  # type: ignore[union-attr]

    def test_notification_escapes_html_and_includes_listing(self):
        message = parse_webhook(
            _event(
                id="m",
                chat_id="chat <1>",
                author_id=2,
                user_id=1,
                type="text",
                created="2026-11-18T07:30:00Z",
                content={"text": "<script>"},
            )
        )
        assert message is not None
        text = format_notification(
            message,
            {
                "context": {
                    "value": {
                        "title": "Диван <новый>",
                        "url": "https://www.avito.ru/item?a=1&b=2",
                        "price_string": "1000 ₽",
                    }
                },
                "users": [{"id": 2, "name": "Анна & Ко"}],
            },
        )
        self.assertIn("Диван &lt;новый&gt;", text)
        self.assertIn("a=1&amp;b=2", text)
        self.assertIn("Анна &amp; Ко", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        created = format_created(message.created)
        assert created is not None
        self.assertIn(created, text)

    def test_historical_uses_timestamp(self):
        old = parse_webhook(
            _event(id="1", chat_id="c", created=1_000, content={"text": "a"})
        )
        fresh = parse_webhook(
            _event(
                id="2",
                chat_id="c",
                created=datetime.now(timezone.utc).timestamp(),
                content={"text": "b"},
            )
        )
        unknown = parse_webhook(_event(id="3", chat_id="c", content={"text": "c"}))
        assert old and fresh and unknown
        started = datetime.now(timezone.utc).timestamp()
        self.assertTrue(is_historical(old, started))
        self.assertFalse(is_historical(fresh, started))
        self.assertTrue(is_historical(unknown, started))


if __name__ == "__main__":
    unittest.main()
