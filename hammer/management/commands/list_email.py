"""List senders of the latest unread messages over IMAP, read-only."""

from asyncio import run
from collections.abc import Callable, Coroutine
from email import message_from_bytes
from functools import wraps
from logging import getLogger
from os import environ
from re import split
from typing import Any, Concatenate
from urllib.parse import unquote, urlparse

from aioimaplib import IMAP4_SSL
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand

logger = getLogger(__name__)
type Runner[**P, T] = Callable[P, Coroutine[Any, Any, T | None]]
type Session[**P, T] = Callable[Concatenate[IMAP4_SSL, P], Coroutine[Any, Any, T]]


def with_imap[**P, T](off: T | None = None) -> Callable[[Session[P, T]], Runner[P, T]]:
    """Supply a client logged in to SPAMHAMMER_EMAIL_URLS, smoke tested by LOGIN.

    URLs, delimited by commas and/or whitespace, starting with 'off' return `off` instead.
    """

    def decorate(function: Session[P, T]) -> Runner[P, T]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T | None:
            urls = environ.get('SPAMHAMMER_EMAIL_URLS', 'off')
            match [url for url in split(r'[\s,]+', urls) if url]:
                case []:
                    raise ImproperlyConfigured('SPAMHAMMER_EMAIL_URLS empty; unset or off skips')
                case [url] if url.startswith('off'):
                    logger.info('SPAMHAMMER_EMAIL_URLS off')
                    return off
                case [url]:
                    parsed = urlparse(url)
                    imap = IMAP4_SSL(parsed.hostname or '', parsed.port or 993)
                    await imap.wait_hello_from_server()
                    login = await imap.login(
                        unquote(parsed.username or ''), unquote(parsed.password or '')
                    )
                    if login.result != 'OK':
                        raise ConnectionError(f'{parsed.hostname} LOGIN {login.result}')
                    logger.info('%s LOGIN OK', parsed.hostname)
                    try:
                        return await function(imap, *args, **kwargs)
                    finally:
                        await imap.logout()
                case _:
                    raise NotImplementedError('exactly one SPAMHAMMER_EMAIL_URLS supported for now')

        return inner

    return decorate


@with_imap(off=[])
async def list_email(imap: IMAP4_SSL) -> list[str]:
    """Return senders of the last 10 unread messages, peeking to leave them unread."""
    await imap.select('INBOX')
    numbers = (await imap.search('UNSEEN')).lines[0].decode().split()[-10:]
    lines = (
        (await imap.fetch(','.join(numbers), 'BODY.PEEK[HEADER.FIELDS (FROM)]')).lines
        if numbers
        else []
    )
    # TODO: cross reference senders against contacts, e.g. CardDAV
    return [
        message['From']
        for message in (message_from_bytes(line) for line in lines if isinstance(line, bytearray))
    ]


class Command(BaseCommand):
    help = __doc__

    def handle(self, *_args: object, **_options: object) -> None:
        """Print one sender per line."""
        print(*run(list_email()) or [], sep='\n')  # noqa: T201 stdout is the interface
