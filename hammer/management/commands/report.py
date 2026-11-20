"""Report spam to Spamhaus."""

from asyncio import run
from logging import getLogger
from os import environ, getpid

from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.hammer import report_after_delay
from hammer.management.commands.sweep import sweep_after

logger = getLogger(__name__)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take one spam argument, an optional reason, and a cancellation window."""
        parser.add_argument('spam', help='sender address, then optionally a space and subject')
        parser.add_argument(
            '--delay-seconds', default=60, help='to cancel before reporting', type=int
        )
        parser.add_argument('--reason', help='for Spamhaus; blank or omitted names the sender')

    def handle(
        self, *_args: object, delay_seconds: int, reason: str | None, spam: str, **_options: object
    ) -> None:
        """Report matching inbox messages to Spamhaus."""
        run(self.handle_async(spam, delay_seconds=delay_seconds, reason=reason))

    async def handle_async(self, spam: str, *, delay_seconds: int, reason: str | None) -> None:
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
        async with sweep_after(sender, dry_run=False, write=self.stdout.write):
            await report_after_delay(
                sender, subject, reason or f'Unsolicited email from {sender}', delay_seconds
            )
