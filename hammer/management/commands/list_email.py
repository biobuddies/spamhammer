"""List sender addresses and subjects of the latest unread messages over IMAP, read-only."""

from asyncio import run
from email import message_from_bytes, policy
from email.utils import parseaddr

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandError

from hammer.hammer import imap_session
from hammer.one_click import find_unsubscribe_url


def describe_email(raw: bytes) -> str:
    """Append a tab-separated marker when the headers support one-click unsubscribe."""
    message = message_from_bytes(raw, policy=policy.default)
    description = f'{parseaddr(message["From"])[1]} {message["Subject"] or ""}'.rstrip()
    try:
        find_unsubscribe_url(raw)
    except (CommandError, ValueError):
        return description
    return f'{description}\t[1C]'


@imap_session(off=[])
async def list_email(imap: IMAP4_SSL) -> list[str]:
    """Return `address subject` of the last 10 unread messages, peeking to leave them unread.

    The first tab-separated field suits hammer's spam argument.
    """
    await imap.select('INBOX')
    if not (numbers := (await imap.search('UNSEEN')).lines[0].decode().split()[-10:]):
        return []
    fetched = await imap.fetch(
        ','.join(numbers),
        'BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)]',
    )
    # TODO: cross reference senders against contacts, e.g. CardDAV
    return [describe_email(bytes(line)) for line in fetched.lines if isinstance(line, bytearray)]


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Print sender and subject with an optional one-click unsubscribe marker."""
        print(*run(list_email()) or [], sep='\n')
