"""Mark matching inbox messages read and move them to Junk."""

from asyncio import run
from collections import Counter
from collections.abc import Sequence
from email import message_from_bytes, policy
from email.utils import parseaddr
from email.utils import quote as escape_quotes
from os import environ
from re import search

from aioimaplib import IMAP4_SSL, Response
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.management.commands.list_email import imap_session


def quote(value: str) -> str:
    """Quote an IMAP string without permitting command injection."""
    if any(character in value for character in '\r\n\0'):
        raise CommandError('IMAP arguments cannot contain control characters')
    return f'"{escape_quotes(value)}"'


def cleanup_scope(sender: str) -> str:
    """Use the exact domain with two or more dots, otherwise the exact sender."""
    if sender.count('@') != 1 or not all(sender.split('@')):
        raise CommandError('Expected a sender address')
    quote(sender)
    domain = sender.rpartition('@')[2].casefold()
    return domain if domain.count('.') >= 2 else sender


def match_messages(lines: Sequence[bytes | bytearray], scope: str) -> dict[str, str]:
    """Map matching UIDs to exact parsed addresses, regardless of UID/literal order."""
    matches = {}
    number = raw = None
    for line in lines:
        if isinstance(line, bytearray):
            raw = line
        else:
            if search(rb'^\d+ FETCH ', line):
                if raw is not None:
                    raise CommandError('FETCH message missing UID')
                number = None
            if found := search(rb'\bUID (\d+)\b', line):
                number = found[1].decode()
        if number is not None and raw is not None:
            message = message_from_bytes(raw, policy=policy.default)
            address = parseaddr(message['From'] or '')[1]
            target = address if '@' in scope else address.rpartition('@')[2]
            if target.casefold() == scope.casefold():
                matches[number] = address
            number = raw = None
    if raw is not None:
        raise CommandError('FETCH message missing UID')
    return matches


def require_ok(response: Response, operation: str) -> None:
    """Stop after a failed IMAP operation."""
    if response.result != 'OK':
        raise CommandError(f'{operation}: {response.result}')


@imap_session(off=Counter())
async def junk_email(
    imap: IMAP4_SSL, sender: str, *, dry_run: bool, folder: str | None = None
) -> Counter[str]:
    """Check exact From addresses, then add Seen and UID MOVE only the matched messages."""
    scope = cleanup_scope(sender)
    folder = folder or environ.get('SPAMHAMMER_JUNK_FOLDER', 'Junk')
    quoted_folder = quote(folder)
    if folder.casefold() == 'inbox':
        raise CommandError('Destination cannot be INBOX')
    selected = await imap.select('INBOX')
    require_ok(selected, 'INBOX SELECT')
    value = scope if '@' in scope else '@' + scope
    found = await imap.uid_search('FROM', quote(value))
    require_ok(found, 'UID SEARCH')
    numbers = found.lines[0].decode().split()
    if not numbers:
        return Counter()
    fetched = await imap.uid('FETCH', ','.join(numbers), '(UID BODY.PEEK[HEADER.FIELDS (FROM)])')
    require_ok(fetched, 'UID FETCH')
    matches = match_messages(fetched.lines, scope)
    counts = Counter(matches.values())
    if dry_run or not matches:
        return counts
    if not imap.protocol or 'MOVE' not in imap.protocol.capabilities:
        raise CommandError('Server must support UID MOVE; no flags changed')
    destination = await imap.status(quoted_folder, '(UIDVALIDITY)')
    require_ok(destination, f'Destination {folder!r} unavailable; no flags changed')
    sequence = ','.join(matches)
    marked = await imap.uid('STORE', sequence, '+FLAGS.SILENT', '(\\Seen)')
    require_ok(marked, 'UID STORE failed; no MOVE attempted')
    moved = await imap.uid('MOVE', sequence, quoted_folder)
    if moved.result != 'OK':
        raise CommandError(
            f'UID MOVE {moved.result}; matching messages may already be read or moved'
        )
    return counts


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a sender and destination; infer cleanup scope from the domain."""
        parser.add_argument('sender')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--folder', default=environ.get('SPAMHAMMER_JUNK_FOLDER', 'Junk'))

    def handle(
        self, *_args: object, sender: str, dry_run: bool, folder: str, **_options: object
    ) -> None:
        """Print the affected message count and sender breakdown."""
        scope = cleanup_scope(sender)
        counts = run(junk_email(sender, dry_run=dry_run, folder=folder)) or Counter()
        addresses = ', '.join(
            f'{address.rpartition("@")[0]}@' for address, _count in counts.most_common()
        )
        suffix = f': {addresses}' if '@' not in scope and counts else ''
        self.stdout.write(
            f'{"Would move" if dry_run else "Moved"} {counts.total()} messages '
            f'from {scope} to {folder}{suffix}'
        )
