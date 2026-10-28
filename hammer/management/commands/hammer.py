"""Report spam to Spamhaus, delete its sender's mail, and filter future mail; checks only so far."""

from asyncio import run
from collections.abc import Callable, Coroutine
from functools import wraps
from logging import getLogger
from os import environ
from typing import Any, Concatenate

from aiohttp import ClientSession, ClientTimeout
from aioimaplib import IMAP4_SSL
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandParser

from hammer.management.commands.list_email import Runner, with_imap

logger = getLogger(__name__)


def with_http[**P, T](
    variable: str, base_url: str, smoke_path: str, off: T | None = None
) -> Callable[[Callable[Concatenate[ClientSession, P], Coroutine[Any, Any, T]]], Runner[P, T]]:
    """Supply a session bearing environ[variable], smoke tested by GET smoke_path.

    Keys starting with 'off', the default, return `off` instead.
    """

    def decorate(
        function: Callable[Concatenate[ClientSession, P], Coroutine[Any, Any, T]],
    ) -> Runner[P, T]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T | None:
            key = environ.get(variable, 'off')
            if not key:
                raise ImproperlyConfigured(f'{variable} empty; unset or off skips')
            if key.startswith('off'):
                logger.info('%s off', variable)
                return off
            async with ClientSession(
                base_url,
                # Default User-Agents get 403 Forbidden from Spamhaus
                headers={'Authorization': f'Bearer {key}', 'User-Agent': 'spamhammer'},
                raise_for_status=True,
                timeout=ClientTimeout(total=30),
            ) as session:
                async with session.get(smoke_path) as response:
                    logger.info(
                        '%s%s st=%s %s',
                        base_url,
                        smoke_path,
                        response.status,
                        await response.json(),
                    )
                return await function(session, *args, **kwargs)

        return inner

    return decorate


@with_imap()
async def delete_and_filter(_imap: IMAP4_SSL, _sender: str) -> None:
    """TODO: delete the sender's mail and add a Sieve rule blocking them."""


@with_http('SPAMHAUS_API_KEY', 'https://submit.spamhaus.org/portal/api/v1/', 'lookup/threats-types')
async def report(_session: ClientSession, _sender: str, _subject: str) -> None:
    """TODO: report spam to Spamhaus."""


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take one spam argument."""
        parser.add_argument('spam', help='sender address, then optionally a space and subject')

    def handle(self, *_args: object, spam: str, **_options: object) -> None:
        """Smoke test IMAP and Spamhaus connectivity."""
        sender, _, subject = spam.partition(' ')
        logger.info('Hammering %s %r', sender, subject)
        # TODO: delay so the workflow can be cancelled (undo) before acting
        run(delete_and_filter(sender))
        run(report(sender, subject))
