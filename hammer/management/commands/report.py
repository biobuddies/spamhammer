"""Report spam to Spamhaus and its sending platform."""

from asyncio import run, to_thread
from email import message_from_bytes
from email.message import Message
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from logging import getLogger
from os import environ, getpid
from re import findall, sub
from smtplib import SMTP
from ssl import create_default_context
from typing import NamedTuple
from urllib.parse import unquote

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.hammer import read_email_urls, report_after_delay
from hammer.management.commands.sweep import sweep_summary

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
        ('exacttarget.com', 'exct.net', 'mc.salesforce.com', 'mktg.salesforce.com'),
    ),
)


def match_platforms(spam: bytes, imap_host: str) -> list[Platform]:
    """Match DKIM passes in the receiving server's topmost Authentication-Results."""
    results = str(message_from_bytes(spam).get('Authentication-Results', ''))
    authority, _, methods = results.partition(';')
    trusted = imap_host.casefold().rstrip('.')
    if not trusted or authority.casefold().split()[:1] not in (
        [trusted],
        [trusted.removeprefix('imap.')],
    ):
        return []
    domains = findall(
        r'(?:^|;)\s*dkim(?:/\d+)?\s*=\s*pass\s+[^;]*?'
        r'\bheader\.d\s*=\s*([a-z0-9.-]+)(?=\s|;|$)',
        sub(r'\([^()]*\)', '', methods).casefold(),
    )
    return [
        platform
        for platform in PLATFORMS
        if any(
            signer == domain or signer.endswith(f'.{domain}')
            for signer in domains
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
            MIMEText(f'{platform.name} signed the attached unsolicited email.\n'),
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
    """Email each platform that sent spam an abuse report, via the submit:// URL."""
    if not (urls := read_email_urls()):
        return
    imap, *others = urls
    if not (platforms := match_platforms(spam, imap.hostname or '')):
        logger.info('No verified DKIM signature from a known platform')
        return
    if not (submission := next(iter(others), None)):
        logger.info(
            'No submit:// URL; skipped %s', ', '.join(platform.name for platform in platforms)
        )
        return
    credentials = submission if submission.username else imap
    with SMTP(submission.hostname or '', submission.port or 587, timeout=30) as smtp:
        smtp.starttls(context=create_default_context())
        smtp.login(unquote(credentials.username or ''), unquote(credentials.password or ''))
        for platform in platforms:
            smtp.send_message(compose_report(unquote(imap.username or ''), platform, spam))
            logger.info('Emailed %s %s', platform.name, platform.mailbox)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a spam argument and a cancellation window."""
        parser.add_argument('spam', help='sender address, then optionally a space and subject')
        parser.add_argument(
            '--delay-seconds', default=60, help='to cancel before reporting', type=int
        )

    def handle(self, *_args: object, delay_seconds: int, spam: str, **_options: object) -> None:
        """Report the latest matching inbox message to Spamhaus and its sending platforms."""
        run(self.handle_async(spam, delay_seconds=delay_seconds))

    async def handle_async(self, spam: str, *, delay_seconds: int) -> None:
        """Report spam and sweep matching mail in one event loop."""
        if delay_seconds < 0:
            raise CommandError('--delay-seconds must be nonnegative')
        sender, _, subject = spam.partition(' ')
        logger.info('Reporting %s %r', sender, subject)
        logger.info(
            'Cancel: %s',
            f'gh run cancel {run_id} --repo {environ["GITHUB_REPOSITORY"]}'
            if (run_id := environ.get('GITHUB_RUN_ID'))
            else f'kill {getpid()}',
        )
        try:
            message, _submission = await report_after_delay(sender, subject, delay_seconds)
            if message:
                await to_thread(email_reports, message)
        except Exception:
            print(await sweep_summary(sender, dry_run=False))
            raise
        print(await sweep_summary(sender, dry_run=False))
