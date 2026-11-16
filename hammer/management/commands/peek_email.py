"""Show the headers and complete message of the latest matching email without marking it read."""

from asyncio import run
from email import message_from_bytes, policy
from email.utils import parseaddr

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.management.commands.list_email import imap_session


@imap_session()
async def fetch_email(imap: IMAP4_SSL, sender: str, subject: str = '') -> bytes:
    """Fetch the latest sender and subject substring match using BODY.PEEK."""
    selected = await imap.select('INBOX')
    if selected.result != 'OK':
        raise CommandError(f'INBOX SELECT {selected.result}')
    found = await imap.uid_search('FROM', f'"{sender}"')
    if found.result != 'OK':
        raise CommandError(f'UID SEARCH {found.result}')
    numbers = found.lines[0].decode().split()
    if numbers:
        fetched = await imap.uid('FETCH', ','.join(numbers), 'BODY.PEEK[]')
        if fetched.result != 'OK':
            raise CommandError(f'UID FETCH {fetched.result}')
        for line in reversed(fetched.lines):
            if not isinstance(line, bytearray):
                continue
            message = message_from_bytes(line, policy=policy.default)
            if parseaddr(message['From'] or '')[1].casefold() == sender.casefold() and (
                not subject or subject in str(message['Subject'] or '')
            ):
                return bytes(line)
    raise CommandError(f'No email from {sender} with subject {subject!r}')


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a sender and optional subject substring."""
        parser.add_argument(
            'email', help='sender address, then optionally a space and subject substring'
        )

    def handle(self, *_args: object, email: str, **_options: object) -> None:
        """Print the complete RFC 5322 message."""
        sender, _, subject = email.partition(' ')
        if message := run(fetch_email(sender, subject)):
            self.stdout.write(message.decode('utf-8', errors='replace'), ending='')
