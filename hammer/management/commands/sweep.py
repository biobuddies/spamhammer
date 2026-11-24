"""Move matching inbox messages to a folder."""

from asyncio import run
from collections import Counter
from collections.abc import Sequence
from email import message_from_bytes, policy
from email.utils import parseaddr
from re import search

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.hammer import imap_session


def cleanup_scope(sender: str) -> str:
    """Use the exact domain with two or more dots, otherwise the exact sender."""
    if sender.count('@') != 1 or not all(sender.split('@')):
        raise CommandError('Expected a sender address')
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


@imap_session(off=Counter(), requires=('MOVE',))
async def sweep(imap: IMAP4_SSL, sender: str, *, dry_run: bool, folder: str) -> Counter[str]:
    """Check exact From addresses, then UID MOVE only the matched messages."""
    scope = cleanup_scope(sender)
    quoted_folder = f'"{folder}"'
    await imap.status(quoted_folder, '(UIDVALIDITY)')
    await imap.select('INBOX')
    value = scope if '@' in scope else '@' + scope
    numbers = (await imap.uid_search('FROM', f'"{value}"')).lines[0].decode().split()
    if not numbers:
        return Counter()
    matches = match_messages(
        (await imap.uid('FETCH', ','.join(numbers), '(UID BODY.PEEK[HEADER.FIELDS (FROM)])')).lines,
        scope,
    )
    counts = Counter(matches.values())
    if dry_run or not matches:
        return counts
    try:
        await imap.uid('MOVE', ','.join(matches), quoted_folder)
    except CommandError as error:
        raise CommandError(f'{error}; some matching messages may already have moved') from error
    return counts


async def sweep_summary(sender: str, *, dry_run: bool, folder: str) -> str:
    """Sweep or preview matching mail and format the result for command output."""
    scope = cleanup_scope(sender)
    counts = await sweep(sender, dry_run=dry_run, folder=folder) or Counter()
    addresses = ', '.join(
        f'{address.rpartition("@")[0]}@' for address, _count in counts.most_common()
    )
    suffix = f': {addresses}' if '@' not in scope and counts else ''
    return (
        f'{"Would move" if dry_run else "Moved"} {counts.total()} messages '
        f'from {scope} to {folder}{suffix}'
    )


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take a sender and infer cleanup scope from the domain."""
        parser.add_argument('sender')
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--folder', default='Spam', help='must already exist')

    def handle(
        self, *_args: object, sender: str, dry_run: bool, folder: str, **_options: object
    ) -> None:
        """Print the affected message count and sender breakdown."""
        print(run(sweep_summary(sender, dry_run=dry_run, folder=folder)))
