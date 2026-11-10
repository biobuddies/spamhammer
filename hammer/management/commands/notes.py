"""Print dated notes of measures taken against senders, newest last, from the Spamhammer folder."""

from asyncio import run
from datetime import UTC, datetime
from email import message_from_bytes, policy
from email.message import EmailMessage

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandParser

from hammer.management.commands.hammer import quote
from hammer.management.commands.list_email import imap_session


@imap_session()
async def record_note(imap: IMAP4_SSL, measure: str, target: str, details: str) -> None:
    """APPEND a read note, e.g. measure `block` against target `example.com`, dated now."""
    note = EmailMessage()
    note['Date'] = now = datetime.now(UTC)
    note['From'] = 'spamhammer@spamhammer.invalid'
    note['Subject'] = f'{measure} {target}'
    note['X-Spamhammer-Measure'] = measure
    note['X-Spamhammer-Target'] = target
    note.set_content(details)
    # CREATE answers NO once the folder exists; APPEND then reports any real problem
    await imap.create('Spamhammer')
    # Cyrus rejects bare LF line endings
    appended = await imap.append(note.as_bytes(policy=policy.SMTP), 'Spamhammer', r'\Seen', now)
    if appended.result != 'OK':
        raise ConnectionError(f'Spamhammer APPEND {appended.result} {appended.lines}')


@imap_session(off=[])
async def read_notes(imap: IMAP4_SSL, target: str) -> list[str]:
    """Return `datetime measure target: details` for notes whose target contains `target`."""
    if (await imap.select('Spamhammer')).result != 'OK':
        return []
    criteria = ('HEADER', 'X-Spamhammer-Target', quote(target)) if target else ('ALL',)
    numbers = (await imap.search(*criteria)).lines[0].decode().split()
    lines = (await imap.fetch(','.join(numbers), 'BODY.PEEK[]')).lines if numbers else []
    notes = [
        message_from_bytes(line, policy=policy.default)
        for line in lines
        if isinstance(line, bytearray)
    ]
    return [
        f'{note["Date"].datetime.isoformat()} {note["Subject"]}: {note.get_content().strip()}'
        for note in notes
    ]


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take an optional target substring."""
        parser.add_argument('target', default='', help='e.g. thealphapaths.com', nargs='?')

    def handle(self, *_args: object, target: str, **_options: object) -> None:
        """Print one note per line."""
        if notes := run(read_notes(target)):
            print(*notes, sep='\n')
