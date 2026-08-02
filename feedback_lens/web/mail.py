from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from feedback_lens.web.config import get_web_settings


@dataclass(frozen=True)
class SentEmail:
    recipient: str
    subject: str
    body: str


MEMORY_OUTBOX: list[SentEmail] = []


class EmailSender:
    def send(self, recipient: str, subject: str, body: str) -> str | None:
        raise NotImplementedError


class MemoryEmailSender(EmailSender):
    def send(self, recipient: str, subject: str, body: str) -> str:
        MEMORY_OUTBOX.append(SentEmail(recipient, subject, body))
        return f"memory-{len(MEMORY_OUTBOX)}"


class ConsoleEmailSender(EmailSender):
    def send(self, recipient: str, subject: str, body: str) -> None:
        print("\n=== Feedback Lens email ===")
        print(f"To: {recipient}")
        print(f"Subject: {subject}")
        print(body)
        print("=== End email ===\n")
        return None


class SmtpEmailSender(EmailSender):
    def __init__(self) -> None:
        self.host = os.environ.get("FEEDBACK_LENS_SMTP_HOST", "").strip()
        self.port = int(os.environ.get("FEEDBACK_LENS_SMTP_PORT", "587"))
        self.username = os.environ.get("FEEDBACK_LENS_SMTP_USERNAME")
        self.password = os.environ.get("FEEDBACK_LENS_SMTP_PASSWORD")
        self.sender = os.environ.get("FEEDBACK_LENS_MAIL_FROM", "").strip()
        self.use_tls = os.environ.get(
            "FEEDBACK_LENS_SMTP_STARTTLS",
            "1",
        ) not in {"0", "false", "False"}
        if not self.host or not self.sender:
            raise RuntimeError(
                "SMTP mail requires FEEDBACK_LENS_SMTP_HOST and "
                "FEEDBACK_LENS_MAIL_FROM."
            )

    def send(self, recipient: str, subject: str, body: str) -> str | None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=30) as client:
            if self.use_tls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password or "")
            response = client.send_message(message)
        return None if not response else str(response)


def get_email_sender() -> EmailSender:
    backend = get_web_settings().mail_backend
    if backend == "memory":
        return MemoryEmailSender()
    if backend == "console":
        return ConsoleEmailSender()
    if backend == "smtp":
        return SmtpEmailSender()
    raise RuntimeError("Outbound email is disabled.")


def mail_is_configured() -> bool:
    settings = get_web_settings()
    if settings.mail_backend in {"memory", "console"}:
        return True
    if settings.mail_backend == "smtp":
        return bool(
            os.environ.get("FEEDBACK_LENS_SMTP_HOST")
            and os.environ.get("FEEDBACK_LENS_MAIL_FROM")
        )
    return False
