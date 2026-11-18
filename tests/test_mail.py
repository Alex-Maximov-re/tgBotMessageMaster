import tempfile
import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

from app.config import Settings
from app.mail import (
    MailLetter,
    MailPoller,
    encode_imap_utf7,
    format_mail_notification,
    is_message_notification,
    parse_raw_email,
    parse_search_uids,
    should_notify,
)
from app.store import SeenStore


def _raw_message(subject: str, sender: str, html_body: str, when: datetime) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = format_datetime(when)
    message.set_content(html_body, subtype="html")
    return message.as_bytes()


class ParseTests(unittest.TestCase):
    def test_html_letter_keeps_text_and_link(self):
        when = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
        raw = _raw_message(
            "Вам написали новое сообщение",
            "Avito <noreply@avito.ru>",
            (
                "<p>Анна: цена &lt;1000&gt;</p>"
                '<p><a href="https://www.avito.ru/profile/messenger?chat=1&amp;x=2">Открыть</a></p>'
                "<p>Отписаться от уведомлений</p>"
            ),
            when,
        )
        letter = parse_raw_email("mail:1:5", raw)
        self.assertEqual(letter.subject, "Вам написали новое сообщение")
        self.assertIn("цена <1000>", letter.text)
        self.assertNotIn("Отписаться", letter.text)
        self.assertEqual(
            letter.link,
            "https://www.avito.ru/profile/messenger?chat=1&x=2",
        )
        self.assertTrue(should_notify(letter, accept_all=False))
        note = format_mail_notification(letter)
        self.assertIn("цена &lt;1000&gt;", note)
        self.assertIn("chat=1&amp;x=2", note)
        self.assertNotIn("<1000>", note)

    def test_promo_is_not_a_chat_notification(self):
        self.assertFalse(
            is_message_notification("Скидки на продвижение", "Успейте купить", accept_all=False)
        )
        self.assertTrue(
            is_message_notification("Вам написали", "", accept_all=False)
        )

    def test_imap_helpers(self):
        self.assertEqual(encode_imap_utf7("INBOX"), "INBOX")
        self.assertEqual(encode_imap_utf7("a&b"), "a&-b")
        self.assertEqual(encode_imap_utf7("А"), "&BBA-")
        self.assertEqual(parse_search_uids([b"3 1 2"]), ["3", "1", "2"])
        self.assertEqual(parse_search_uids([b""]), [])
        self.assertEqual(parse_search_uids([None]), [])

    def test_host_is_guessed_from_mailbox(self):
        self.assertEqual(
            Settings(imap_user="seller@ya.ru").imap_host_resolved(),
            "imap.yandex.ru",
        )
        self.assertEqual(
            Settings(imap_user="seller@ya.ru", imap_host="imap.example").imap_host_resolved(),
            "imap.example",
        )
        with self.assertRaises(SystemExit):
            Settings(imap_user="seller@example.com").imap_host_resolved()


class _Telegram:
    def __init__(self, fail_times: int = 0):
        self.sent: list[str] = []
        self.fail_times = fail_times

    async def send(self, text: str) -> None:
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        self.sent.append(text)


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_pass_sends_only_a_fresh_chat_letter(self):
        old = MailLetter(
            store_id="mail:1:1",
            subject="Вам написали",
            sender="Avito <noreply@avito.ru>",
            text="старое",
            link=None,
            received=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        fresh = MailLetter(
            store_id="mail:1:2",
            subject="Новое сообщение",
            sender="Avito <noreply@avito.ru>",
            text="свежее",
            link="https://www.avito.ru/profile/messenger",
            received=datetime.now(timezone.utc),
        )
        promo = MailLetter(
            store_id="mail:1:3",
            subject="Скидки на продвижение",
            sender="Avito <noreply@avito.ru>",
            text="акция",
            link=None,
            received=datetime.now(timezone.utc),
        )
        letters = [old, fresh, promo]

        def fetch(_settings, known):
            return [item for item in letters if item.store_id not in known]

        with tempfile.TemporaryDirectory() as folder:
            telegram = _Telegram()
            poller = MailPoller(
                Settings(),
                telegram,
                SeenStore(Path(folder) / "state.json"),
                fetch=fetch,
            )
            await poller.poll_once(seed_only=True)
            await poller.poll_once(seed_only=False)
        self.assertEqual(len(telegram.sent), 1)
        self.assertIn("свежее", telegram.sent[0])

    async def test_failed_send_is_retried(self):
        letter = MailLetter(
            store_id="mail:1:9",
            subject="Вам написали",
            sender="noreply@avito.ru",
            text="ещё раз",
            link=None,
            received=datetime.now(timezone.utc),
        )

        def fetch(_settings, known):
            return [] if letter.store_id in known else [letter]

        with tempfile.TemporaryDirectory() as folder:
            telegram = _Telegram(fail_times=1)
            poller = MailPoller(
                Settings(),
                telegram,
                SeenStore(Path(folder) / "state.json"),
                fetch=fetch,
            )
            await poller.poll_once(seed_only=False)
            await poller.poll_once(seed_only=False)
        self.assertEqual(telegram.sent, [format_mail_notification(letter)])


if __name__ == "__main__":
    unittest.main()
