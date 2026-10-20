"""Booking emails: what is sent to whom for each booking event, and the record of every attempt.

The message builders are pure: they take a :class:`Context` and return messages. :class:`Notifier` loads
the context from the database, sends through ``mail.Mailer`` and records each attempt in ``email_log``.
It never raises: a failed email is logged and the booking change stands.

Who receives what:
    * ``confirmed``: the customer (details, calendar file, manage link) and the business's contact email.
    * ``cancelled``: the customer, and the business unless the business cancelled it itself.
    * ``moved``: the customer (new time, updated calendar file, manage link) and the business.

Every value that came from a person is HTML-escaped before it is placed in an HTML body. The log holds no
message body and no address.
"""

import html
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from mail import Attachment, Mailer, MailError, Message
from postgres import Database, DatabaseUnavailableError

LOGGER = logging.getLogger(__name__)

SITE_URL_ENV = "PUBLIC_SITE_URL"
_SITE_PATTERN = re.compile(r"^(https://[a-z0-9.-]+|http://(localhost|127\.0\.0\.1)(:\d{1,5})?)(/[A-Za-z0-9._~/-]*)?$")
KINDS = ("confirmed", "cancelled", "moved")


class NotifyConfigError(RuntimeError):
    """Raised when ``PUBLIC_SITE_URL`` is set but unusable."""


@dataclass(frozen=True)
class Context:
    """Everything the messages for one booking event need.

    Attributes:
        kind: ``confirmed``, ``cancelled`` or ``moved``.
        business_name: The business's display name.
        business_slug: The business's public identifier.
        time_zone: IANA time zone for showing times.
        contact_email: Where the business gets its emails, or ``None``.
        meeting_link: The business's meeting link, or ``None``.
        reference: Booking reference.
        service_name: The booked service.
        starts_at: Start, aware.
        ends_at: End, aware.
        customer_name: Customer name.
        customer_email: Customer email.
        customer_phone: Customer phone, or ``None``.
        customer_note: Customer note, or ``None``.
        cancelled_by: ``customer``, ``owner`` or ``None``.
        sequence: How many times the booking has been moved, for calendar updates.
        manage_url: The private manage link, or ``None`` when it cannot be included.
    """

    kind: str
    business_name: str
    business_slug: str
    time_zone: str
    contact_email: str | None
    meeting_link: str | None
    reference: str
    service_name: str
    starts_at: datetime
    ends_at: datetime
    customer_name: str
    customer_email: str
    customer_phone: str | None
    customer_note: str | None
    cancelled_by: str | None
    sequence: int
    manage_url: str | None


def manage_url(site_url: str, slug: str, reference: str, token: str) -> str:
    """Build a manage link. The code travels after ``#``, so it never reaches a server's logs.

    Args:
        site_url: The public site, without a trailing slash.
        slug: Business slug.
        reference: Booking reference.
        token: Manage code.

    Returns:
        The link.
    """
    return f"{site_url}/#manage={slug}.{reference}.{token}"


def when_text(context: Context) -> str:
    """Describe the session time in the business's time zone.

    Args:
        context: The event.

    Returns:
        Text such as ``Tue 3 Nov 2026, 09:00 to 09:30 (UK time)``.
    """
    zone = ZoneInfo(context.time_zone)
    start = context.starts_at.astimezone(zone)
    end = context.ends_at.astimezone(zone)
    label = "UK time" if context.time_zone == "Europe/London" else context.time_zone
    return f"{start:%a} {start.day} {start:%b %Y}, {start:%H:%M} to {end:%H:%M} ({label})"


def _ics_text(value: str) -> str:
    """Escape text for an iCalendar property.

    Args:
        value: The text.

    Returns:
        The escaped text.
    """
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def calendar_file(context: Context, sender_domain: str = "example.com") -> bytes:
    """Build an iCalendar file for the booking: a new or updated event, or a cancellation.

    Args:
        context: The event.
        sender_domain: Domain used in the event's unique identifier.

    Returns:
        The ``.ics`` bytes, with CRLF line endings.
    """
    cancelled = context.kind == "cancelled"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    description = f"Booking reference {context.reference}"
    if context.meeting_link and not cancelled:
        description += f". Join: {context.meeting_link}"
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Booking platform//EN",
        f"METHOD:{'CANCEL' if cancelled else 'PUBLISH'}",
        "BEGIN:VEVENT",
        f"UID:{context.reference}@{sender_domain}",
        f"SEQUENCE:{context.sequence + (1 if cancelled else 0)}",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{context.starts_at.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"DTEND:{context.ends_at.astimezone(UTC):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{_ics_text(context.service_name + ' with ' + context.business_name)}",
        f"DESCRIPTION:{_ics_text(description)}",
        f"STATUS:{'CANCELLED' if cancelled else 'CONFIRMED'}",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _rows_html(rows: list[tuple[str, str]]) -> str:
    """Render label/value rows as an HTML table, escaping every value.

    Args:
        rows: ``(label, value)`` pairs.

    Returns:
        The table markup.
    """
    cells = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#595959">{html.escape(label)}</td>'
        f'<td style="padding:4px 0"><strong>{html.escape(value)}</strong></td></tr>'
        for label, value in rows
    )
    return f'<table style="border-collapse:collapse;font-size:15px">{cells}</table>'


def _page(title: str, intro: str, rows: list[tuple[str, str]], link: tuple[str, str] | None, footer: str) -> str:
    """Wrap an email body in simple, readable HTML.

    Args:
        title: Heading. Escaped here.
        intro: First paragraph. Escaped here.
        rows: Details. Escaped here.
        link: ``(label, url)`` for one button, or ``None``. Both escaped here.
        footer: Closing line. Escaped here.

    Returns:
        The HTML document.
    """
    button = ""
    if link is not None:
        button = (
            f'<p style="margin:24px 0"><a href="{html.escape(link[1], quote=True)}" '
            'style="background:#1f4e5f;color:#ffffff;padding:10px 18px;border-radius:8px;text-decoration:none">'
            f"{html.escape(link[0])}</a></p>"
        )
    return (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;color:#1a1a1a;'
        'max-width:560px;margin:0 auto;padding:24px">'
        f'<h1 style="font-size:22px">{html.escape(title)}</h1><p>{html.escape(intro)}</p>'
        f'{_rows_html(rows)}{button}<p style="color:#595959;font-size:13px">{html.escape(footer)}</p>'
        "</body></html>"
    )


def _text(title: str, intro: str, rows: list[tuple[str, str]], link: tuple[str, str] | None, footer: str) -> str:
    """Render the plain-text version of an email.

    Args:
        title: Heading.
        intro: First paragraph.
        rows: Details.
        link: ``(label, url)`` or ``None``.
        footer: Closing line.

    Returns:
        The text body.
    """
    lines = [title, "", intro, ""] + [f"{label}: {value}" for label, value in rows]
    if link is not None:
        lines += ["", f"{link[0]}: {link[1]}"]
    return "\n".join(lines + ["", footer]) + "\n"


def customer_message(context: Context, sender_domain: str = "example.com") -> Message:
    """Build the customer's email for an event.

    Args:
        context: The event.
        sender_domain: Domain for the calendar event's identifier, normally the sending domain.

    Returns:
        The message, with a calendar file attached.
    """
    rows = [("Service", context.service_name), ("When", when_text(context)), ("Reference", context.reference)]
    if context.meeting_link and context.kind != "cancelled":
        rows.append(("Meeting link", context.meeting_link))
    link = ("Manage your booking", context.manage_url) if context.manage_url and context.kind != "cancelled" else None
    if context.kind == "confirmed":
        subject = f"Booking confirmed: {context.service_name} with {context.business_name}"
        title, intro = (
            "You are booked",
            f"Hello {context.customer_name}, your booking with {context.business_name} is confirmed.",
        )
    elif context.kind == "moved":
        subject = f"Booking moved: {context.service_name} with {context.business_name}"
        title, intro = "Your booking has moved", f"Hello {context.customer_name}, your booking now has a new time."
    else:
        subject = f"Booking cancelled: {context.service_name} with {context.business_name}"
        who = "by the business" if context.cancelled_by == "owner" else "as you asked"
        title, intro = "Booking cancelled", f"Hello {context.customer_name}, your booking has been cancelled {who}."
    footer = (
        "Use the button to move or cancel. Keep this email private: anyone with the link can change the booking."
        if link
        else f"Questions? Reply to this email to reach {context.business_name}."
    )
    return Message(
        to=context.customer_email,
        subject=subject,
        html=_page(title, intro, rows, link, footer),
        text=_text(title, intro, rows, link, footer),
        reply_to=context.contact_email,
        attachments=[
            Attachment(f"booking-{context.reference}.ics", calendar_file(context, sender_domain), "text/calendar")
        ],
    )


def owner_message(context: Context) -> Message | None:
    """Build the business's email for an event, or ``None`` when none is due.

    Args:
        context: The event.

    Returns:
        The message, or ``None`` when there is no contact email or the business made the change itself.
    """
    if not context.contact_email or (context.kind == "cancelled" and context.cancelled_by == "owner"):
        return None
    verb = {"confirmed": "New booking", "cancelled": "Booking cancelled", "moved": "Booking moved"}[context.kind]
    rows = [
        ("Service", context.service_name),
        ("When", when_text(context)),
        ("Reference", context.reference),
        ("Customer", context.customer_name),
        ("Email", context.customer_email),
        ("Phone", context.customer_phone or "-"),
        ("Note", context.customer_note or "-"),
    ]
    title = f"{verb}: {context.customer_name}"
    intro = f"{verb} for {context.business_name}."
    footer = "Reply to this email to contact the customer."
    return Message(
        to=context.contact_email,
        subject=f"{verb}: {context.service_name}, {when_text(context)}",
        html=_page(title, intro, rows, None, footer),
        text=_text(title, intro, rows, None, footer),
        reply_to=context.customer_email,
    )


def site_url_from_environment(environ: Mapping[str, str]) -> str | None:
    """Read the public site URL that manage links point to.

    Args:
        environ: The mapping to read.

    Returns:
        The URL without a trailing slash, or ``None`` when absent.

    Raises:
        NotifyConfigError: If set but not an ``https://`` URL (``http://localhost`` is allowed for development).
    """
    raw = environ.get(SITE_URL_ENV, "").strip().rstrip("/")
    if not raw:
        return None
    if not _SITE_PATTERN.match(raw):
        raise NotifyConfigError(f"{SITE_URL_ENV} must be an https:// URL such as https://book.example.com.")
    return raw


_CONTEXT_QUERY = (
    "SELECT b.business_id::text, z.name, z.slug, z.time_zone, z.contact_email, z.meeting_link, b.reference, s.name, "
    "b.starts_at, b.ends_at, b.customer_name, b.customer_email, b.customer_phone, b.customer_note, b.cancelled_by, "
    "b.move_count FROM bookings b JOIN businesses z ON z.id = b.business_id JOIN services s ON s.id = b.service_id "
    "WHERE b.id = %s"
)


class Notifier:
    """Sends booking emails and records each attempt. Never raises."""

    def __init__(
        self, database: Database, mailer: Mailer | None, site_url: str | None, sender_domain: str = "example.com"
    ) -> None:
        """Create the notifier. Performs no I/O.

        Args:
            database: The database boundary.
            mailer: The email provider, or ``None`` to record every email as skipped.
            site_url: Where manage links point, or ``None`` to leave links out.
            sender_domain: Domain for calendar event identifiers, normally the sending domain.
        """
        self._database = database
        self._mailer = mailer
        self._site_url = site_url
        self._sender_domain = sender_domain

    def _context(self, kind: str, booking_id: str, token: str | None) -> tuple[str, Context] | None:
        """Load what the messages need.

        Args:
            kind: The event kind.
            booking_id: The booking.
            token: The manage code, if held.

        Returns:
            ``(business_id, context)``, or ``None`` if the booking is gone.
        """
        with self._database.connection() as connection:
            row: Any = connection.execute(_CONTEXT_QUERY, (booking_id,)).fetchone()
        if row is None:
            return None
        link = manage_url(self._site_url, row[2], row[6], token) if self._site_url and token else None
        context = Context(
            kind=kind,
            business_name=row[1],
            business_slug=row[2],
            time_zone=row[3],
            contact_email=row[4],
            meeting_link=row[5],
            reference=row[6],
            service_name=row[7],
            starts_at=row[8],
            ends_at=row[9],
            customer_name=row[10],
            customer_email=row[11],
            customer_phone=row[12],
            customer_note=row[13],
            cancelled_by=row[14],
            sequence=int(row[15]),
            manage_url=link,
        )
        return str(row[0]), context

    def _record(
        self, business_id: str, booking_id: str, kind: str, role: str, status: str, provider_id: str, error: str
    ) -> None:
        """Record one attempt in the email log. Failures to record are logged, not raised.

        Args:
            business_id: The business.
            booking_id: The booking.
            kind: The event kind.
            role: ``customer`` or ``owner``.
            status: ``sent``, ``failed`` or ``skipped``.
            provider_id: The provider's message identifier, if sent.
            error: A short status, if failed.
        """
        try:
            with self._database.connection() as connection:
                connection.execute(
                    "INSERT INTO email_log (business_id, booking_id, kind, recipient_role, status, provider_id, error) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (business_id, booking_id, kind, role, status, provider_id or None, error[:200] or None),
                )
        except Exception as exc:  # noqa: BLE001 (recording must never break the caller)
            LOGGER.warning("email log write failed (%s)", type(exc).__name__)

    def booking_event(self, kind: str, booking_id: str, manage_token: str | None) -> None:
        """Send the emails for one booking event and record the attempts. Never raises.

        Args:
            kind: ``confirmed``, ``cancelled`` or ``moved``.
            booking_id: The booking.
            manage_token: The manage code, if the caller holds it.
        """
        if kind not in KINDS:
            return
        try:
            loaded = self._context(kind, booking_id, manage_token)
        except (DatabaseUnavailableError, Exception) as exc:  # noqa: BLE001
            LOGGER.warning("email context unavailable (%s)", type(exc).__name__)
            return
        if loaded is None:
            return
        business_id, context = loaded
        outgoing: list[tuple[str, Message | None]] = [
            ("customer", customer_message(context, self._sender_domain)),
            ("owner", owner_message(context)),
        ]
        for role, message in outgoing:
            if message is None:
                continue
            if self._mailer is None:
                self._record(business_id, booking_id, kind, role, "skipped", "", "email not configured")
                continue
            try:
                provider_id = self._mailer.send(message)
            except MailError as exc:
                LOGGER.warning("email to %s failed: %s", role, exc)
                self._record(business_id, booking_id, kind, role, "failed", "", str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("email to %s failed (%s)", role, type(exc).__name__)
                self._record(business_id, booking_id, kind, role, "failed", "", type(exc).__name__)
                continue
            self._record(business_id, booking_id, kind, role, "sent", provider_id, "")
