"""List senders of the latest unread messages over IMAP, read-only."""

from asyncio import run
from email import message_from_bytes
from os import environ
from re import split
from urllib.parse import unquote, urlparse

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand


async def list_email(url: str) -> list[str]:
    """Return senders of the last 10 unread messages, peeking to leave them unread."""
    parsed = urlparse(url)
    imap = IMAP4_SSL(parsed.hostname or '', parsed.port or 993)
    await imap.wait_hello_from_server()
    await imap.login(unquote(parsed.username or ''), unquote(parsed.password or ''))
    await imap.select('INBOX')
    numbers = (await imap.search('UNSEEN')).lines[0].decode().split()[-10:]
    lines = (
        (await imap.fetch(','.join(numbers), 'BODY.PEEK[HEADER.FIELDS (FROM)]')).lines
        if numbers
        else []
    )
    await imap.logout()
    # TODO: cross reference senders against contacts, e.g. CardDAV
    return [
        message['From']
        for message in (message_from_bytes(line) for line in lines if isinstance(line, bytearray))
    ]


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Read SPAMHAMMER_EMAIL_URLS, delimited by commas and/or whitespace."""
        match [url for url in split(r'[\s,]+', environ['SPAMHAMMER_EMAIL_URLS']) if url]:
            case [url]:
                self.stdout.write('\n'.join(run(list_email(url))))
            case _:
                raise NotImplementedError('exactly one SPAMHAMMER_EMAIL_URLS supported for now')
