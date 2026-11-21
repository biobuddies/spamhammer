"""Report spam to Spamhaus and attributable mail to Salesforce Marketing Cloud."""

from asyncio import run, to_thread
from email import message_from_bytes
from email.message import Message
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from logging import getLogger
from os import environ, getpid
from re import search, sub
from smtplib import SMTP
from ssl import create_default_context
from urllib.parse import unquote

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.hammer import read_email_urls, report_after_delay
from hammer.management.commands.sweep import sweep_summary

logger = getLogger(__name__)

# https://www.salesforce.com/company/legal/abuse/ asks for complete headers.
SALESFORCE_ABUSE = 'abuse@salesforce.com'


def signed_by_salesforce(spam: bytes, imap_host: str) -> bool:
    """Look for a Salesforce DKIM pass in the receiver's topmost Authentication-Results."""
    results = str(message_from_bytes(spam).get('Authentication-Results', ''))
    authority, _, methods = results.partition(';')
    trusted = imap_host.casefold().rstrip('.')
    if not trusted or authority.casefold().split()[:1] not in (
        [trusted],
        [trusted.removeprefix('imap.')],
    ):
        return False
    # Domains from https://www.spamresource.com/2020/07/quick-list-esp-abusespam-contact-list.html
    return bool(
        search(
            r'(?:^|;)\s*dkim(?:/\d+)?\s*=\s*pass\s+[^;]*?'
            r'\bheader\.d\s*=\s*(?:[a-z0-9-]+\.)*'
            r'(?:exacttarget\.com|exct\.net|mc\.salesforce\.com|mktg\.salesforce\.com)(?=\s|;|$)',
            sub(r'\([^()]*\)', '', methods).casefold(),
        )
    )


def compose_report(sender: str, spam: bytes) -> MIMEMultipart:
    """Wrap spam, complete with headers, in an abuse report to Salesforce."""
    original = message_from_bytes(spam)
    feedback = Message()
    feedback['Feedback-Type'] = 'abuse'
    feedback['User-Agent'] = 'spamhammer'
    feedback['Version'] = '1'
    report = MIMEMultipart(
        'report',
        None,
        [
            MIMEText('Salesforce Marketing Cloud signed the attached unsolicited email.\n'),
            MIMEMessage(feedback, 'feedback-report'),
            MIMEMessage(original),
        ],
        report_type='feedback-report',
    )
    report['Date'] = formatdate()
    report['From'] = sender
    report['Message-ID'] = make_msgid(domain=sender.rpartition('@')[2])
    report['Subject'] = f'Abuse report: {original["Subject"] or "unsolicited email"}'
    report['To'] = SALESFORCE_ABUSE
    return report


def send_salesforce_report(spam: bytes) -> None:
    """Send a Salesforce abuse report via the submit:// URL if the signature passes."""
    if not (urls := read_email_urls()):
        return
    imap, *others = urls
    if not signed_by_salesforce(spam, imap.hostname or ''):
        logger.info('No verified Salesforce Marketing Cloud DKIM signature')
        return
    if not (submission := next(iter(others), None)):
        logger.info('No submit:// URL; skipped Salesforce Marketing Cloud')
        return
    credentials = submission if submission.username else imap
    with SMTP(submission.hostname or '', submission.port or 587, timeout=30) as smtp:
        smtp.starttls(context=create_default_context())
        smtp.login(unquote(credentials.username or ''), unquote(credentials.password or ''))
        smtp.send_message(compose_report(unquote(imap.username or ''), spam))
        logger.info('Emailed abuse report to %s', SALESFORCE_ABUSE)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a spam argument and a cancellation window."""
        parser.add_argument('spam', help='sender address, then optionally a space and subject')
        parser.add_argument(
            '--delay-seconds', default=60, help='to cancel before reporting', type=int
        )

    def handle(self, *_args: object, delay_seconds: int, spam: str, **_options: object) -> None:
        """Report the latest matching message to Spamhaus and, when verified, Salesforce."""
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
                await to_thread(send_salesforce_report, message)
        except Exception:
            print(await sweep_summary(sender, dry_run=False))
            raise
        print(await sweep_summary(sender, dry_run=False))
