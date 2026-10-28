"""Report spam to Spamhaus, delete its sender's mail, and filter future mail; checks only so far."""

from asyncio import run
from collections.abc import Callable
from functools import wraps
from json import load
from logging import getLogger
from os import environ
from typing import Concatenate
from urllib.request import Request, urlopen

from aioimaplib import IMAP4_SSL
from django.core.management.base import BaseCommand, CommandParser

from hammer.management.commands.list_email import with_imap

logger = getLogger(__name__)
type Get = Callable[[str], object]


def with_http[**P, T](
    variable: str, base_url: str, smoke_path: str, off: T | None = None
) -> Callable[[Callable[Concatenate[Get, P], T]], Callable[P, T | None]]:
    """Supply a JSON GET function bearing environ[variable], smoke tested on smoke_path.

    Keys starting with 'off', the default, return `off` instead.
    """

    def decorate(function: Callable[Concatenate[Get, P], T]) -> Callable[P, T | None]:
        @wraps(function)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
            key = environ.get(variable, 'off')
            if key.startswith('off'):
                logger.info('%s off', variable)
                return off

            def inner(path: str) -> object:
                request = Request(  # noqa: S310 https base_url
                    base_url + path,
                    # Python-urllib's default User-Agent gets 403 Forbidden from Spamhaus
                    headers={'Authorization': f'Bearer {key}', 'User-Agent': 'spamhammer'},
                )
                with urlopen(request, timeout=30) as response:  # noqa: S310 https base_url
                    return load(response)

            logger.info('%s%s %s', base_url, smoke_path, inner(smoke_path))
            return function(inner, *args, **kwargs)

        return wrapper

    return decorate


@with_imap()
async def delete_and_filter(_imap: IMAP4_SSL, _sender: str) -> None:
    """TODO: delete the sender's mail and add a Sieve rule blocking them."""


@with_http('SPAMHAUS_API_KEY', 'https://submit.spamhaus.org/portal/api/v1/', 'submissions/count')
def report(_get: Get, _sender: str, _subject: str) -> None:
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
        report(sender, subject)
