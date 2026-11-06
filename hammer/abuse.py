"""Email Abuse Reporting Format (RFC 5965) reports to the platforms that sent spam."""

from email import message_from_bytes
from email.message import Message
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from logging import getLogger
from re import findall
from smtplib import SMTP
from ssl import create_default_context
from typing import NamedTuple
from urllib.parse import unquote

from hammer.management.commands.list_email import read_email_urls

logger = getLogger(__name__)


class Platform(NamedTuple):
    name: str
    mailbox: str
    domains: tuple[str, ...]


# Domains from https://www.spamresource.com/2020/07/quick-list-esp-abusespam-contact-list.html
PLATFORMS = (
    # https://www.salesforce.com/company/legal/abuse/ asks for complete headers; Spam Resource
    # lists abuse@abuse.salesforce.com instead
    Platform(
        'Salesforce Marketing Cloud',
        'abuse@salesforce.com',
        ('exacttarget.com', 'exct.net', 'mktg.salesforce.com'),
    ),
)


def match_platforms(spam: bytes) -> list[Platform]:
    """Return platforms owning a hostname in the headers tracing where spam came from."""
    headers = message_from_bytes(spam)
    hostnames = {
        hostname.lower()
        for name in ('DKIM-Signature', 'Message-ID', 'Received', 'Return-Path')
        for value in headers.get_all(name, [])
        for hostname in findall(r'[\w-]+(?:\.[\w-]+)+', str(value))
    }
    return [
        platform
        for platform in PLATFORMS
        if any(
            hostname == domain or hostname.endswith(f'.{domain}')
            for hostname in hostnames
            for domain in platform.domains
        )
    ]


def compose_report(sender: str, platform: Platform, spam: bytes) -> MIMEMultipart:
    """Wrap spam, complete with headers, in an abuse report from sender to platform."""
    original = message_from_bytes(spam)
    feedback = Message()
    feedback['Feedback-Type'] = 'abuse'
    feedback['User-Agent'] = 'spamhammer'
    feedback['Version'] = '1'
    report = MIMEMultipart(
        'report',
        None,
        [
            MIMEText(f'{platform.name} sent the attached unsolicited email.\n'),
            MIMEMessage(feedback, 'feedback-report'),
            MIMEMessage(original),
        ],
        report_type='feedback-report',
    )
    report['Date'] = formatdate()
    report['From'] = sender
    report['Message-ID'] = make_msgid(domain=sender.rpartition('@')[2])
    report['Subject'] = f'Abuse report: {original["Subject"] or "unsolicited email"}'
    report['To'] = platform.mailbox
    return report


def email_reports(spam: bytes) -> None:
    """Email each platform that sent spam an abuse report, via the submission:// URL."""
    if not (platforms := match_platforms(spam)):
        logger.info('No known platform sent it')
        return
    if not (urls := read_email_urls()):
        return
    imap, *others = urls
    if not (submission := next(iter(others), None)):
        logger.info(
            'No submission:// URL; skipped %s', ', '.join(platform.name for platform in platforms)
        )
        return
    credentials = submission if submission.username else imap
    with SMTP(submission.hostname or '', submission.port or 587, timeout=30) as smtp:
        smtp.starttls(context=create_default_context())
        smtp.login(unquote(credentials.username or ''), unquote(credentials.password or ''))
        for platform in platforms:
            smtp.send_message(compose_report(unquote(imap.username or ''), platform, spam))
            logger.info('Emailed %s %s', platform.name, platform.mailbox)
