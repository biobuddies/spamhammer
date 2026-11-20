"""List sender addresses and subjects of the latest unread messages over IMAP, read-only."""

from asyncio import run
from collections.abc import Callable, Coroutine
from email import message_from_bytes, policy
from email.utils import parseaddr
from functools import wraps
from logging import getLogger
from os import environ
from re import split
from typing import Any, Concatenate, override
from urllib.parse import ParseResult, unquote, urlparse

from aioimaplib import IMAP4_SSL, Response
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from hammer.one_click import find_unsubscribe_url

logger = getLogger(__name__)
type Runner[**P, T] = Callable[P, Coroutine[Any, Any, T | None]]
type Session[**P, T] = Callable[Concatenate[IMAP4_SSL, P], Coroutine[Any, Any, T]]


class ImapClient(IMAP4_SSL):
    """Raise on failed login and mailbox commands, leaving STATUS available for probes."""

    @staticmethod
    def _checked(response: Response, operation: str) -> Response:
        if response.result != 'OK':
            raise CommandError(f'{operation} {response.result}')
        return response

    @override
    async def login(self, user: str, password: str) -> Response:
        response = await super().login(user, password)
        if response.result != 'OK':
            raise ConnectionError(f'{self.host} LOGIN {response.result}')
        return response

    @override
    async def select(self, mailbox: str = 'INBOX') -> Response:
        return self._checked(await super().select(mailbox), f'{mailbox} SELECT')

    @override
    async def search(self, *criteria: str, charset: str | None = 'utf-8') -> Response:
        return self._checked(await super().search(*criteria, charset=charset), 'SEARCH')

    @override
    async def uid_search(self, *criteria: str, charset: str | None = 'utf-8') -> Response:
        return self._checked(await super().uid_search(*criteria, charset=charset), 'UID SEARCH')

    @override
    async def fetch(self, message_set: str, message_parts: str) -> Response:
        return self._checked(await super().fetch(message_set, message_parts), 'FETCH')

    @override
    async def uid(self, command: str, *criteria: str) -> Response:
        return self._checked(await super().uid(command, *criteria), f'UID {command}')


def read_email_url() -> ParseResult | None:
    """Parse SPAMHAMMER_EMAIL_URLS, delimited by commas and/or whitespace; None when off."""
    urls = environ.get('SPAMHAMMER_EMAIL_URLS', 'off')
    match [url for url in split(r'[\s,]+', urls) if url]:
        case []:
            raise ImproperlyConfigured('SPAMHAMMER_EMAIL_URLS empty; off skips')
        case [url] if url.startswith('off'):
            logger.info('SPAMHAMMER_EMAIL_URLS off')
            return None
        case [url]:
            return urlparse(url)
        case _:
            raise NotImplementedError('exactly one SPAMHAMMER_EMAIL_URLS supported for now')


def imap_session[**P, T](off: T | None = None) -> Callable[[Session[P, T]], Runner[P, T]]:
    """Supply a client logged in to SPAMHAMMER_EMAIL_URLS, smoke tested by LOGIN.

    URLs, delimited by commas and/or whitespace, starting with 'off' return `off` instead.
    """

    def decorate(function: Session[P, T]) -> Runner[P, T]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T | None:
            if not (parsed := read_email_url()):
                return off
            imap = ImapClient(parsed.hostname or '', parsed.port or 993)
            await imap.wait_hello_from_server()
            await imap.login(unquote(parsed.username or ''), unquote(parsed.password or ''))
            logger.info('%s LOGIN OK', parsed.hostname)
            try:
                return await function(imap, *args, **kwargs)
            finally:
                await imap.logout()

        return inner

    return decorate


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
    found = await imap.search('UNSEEN')
    if not (numbers := found.lines[0].decode().split()[-10:]):
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
