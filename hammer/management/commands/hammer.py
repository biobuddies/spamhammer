"""Report spam to Spamhaus, delete its sender's mail, and filter future mail; checks only so far."""

from asyncio import get_running_loop, run
from collections.abc import Callable, Coroutine
from functools import wraps
from logging import getLogger
from os import environ, getpid
from types import SimpleNamespace
from typing import Any, Concatenate

from aiohttp import (
    ClientSession,
    ClientTimeout,
    TraceConfig,
    TraceRequestEndParams,
    TraceRequestStartParams,
)
from aioimaplib import IMAP4_SSL
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandParser

from hammer.management.commands.list_email import imap_session

logger = getLogger(__name__)
type Json = dict[str, Any]
type JsonSession[**P] = Callable[Concatenate[ClientSession, Any, P], Coroutine[Any, Any, Json]]


async def time_request(
    _session: ClientSession, context: SimpleNamespace, _params: TraceRequestStartParams
) -> None:
    context.start = get_running_loop().time()


async def log_request_end(
    _session: ClientSession, context: SimpleNamespace, params: TraceRequestEndParams
) -> None:
    logger.info(
        '%s %s st=%s rt=%.3fs',
        params.method,
        params.url,
        params.response.status,
        get_running_loop().time() - context.start,
    )


def json_session[**P](
    variable: str, base_url: str, get_first: str, off_response: Json | None = None
) -> Callable[[JsonSession[P]], Callable[P, Coroutine[Any, Any, Json]]]:
    """Supply a session bearing environ[variable], then the JSON from its GET get_first.

    Keys starting with 'off', the default, return off_response, or {} for None, instead.
    """
    # head_first, mutually exclusive with get_first, could suit APIs lacking a useful first GET

    def decorate(function: JsonSession[P]) -> Callable[P, Coroutine[Any, Any, Json]]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> Json:
            key = environ.get(variable, 'off')
            if not key:
                raise ImproperlyConfigured(f'{variable} empty; unset or off skips')
            if key.startswith('off'):
                logger.info('%s off', variable)
                return off_response or {}
            trace = TraceConfig()
            trace.on_request_start.append(time_request)
            trace.on_request_end.append(log_request_end)
            async with ClientSession(
                base_url,
                # Default User-Agents get 403 Forbidden from Spamhaus
                headers={'Authorization': f'Bearer {key}', 'User-Agent': 'spamhammer'},
                raise_for_status=True,
                timeout=ClientTimeout(total=30),
                trace_configs=[trace],
            ) as session:
                async with session.get(get_first) as response:
                    first = await response.json()
                return await function(session, first, *args, **kwargs)

        return inner

    return decorate


@imap_session()
async def delete_and_filter(_imap: IMAP4_SSL, _sender: str) -> None:
    """TODO: delete the sender's mail and add a Sieve rule blocking them."""


@json_session(
    'SPAMHAUS_API_KEY', 'https://submit.spamhaus.org/portal/api/v1/', 'lookup/threats-types'
)
async def report(_session: ClientSession, _threat_types: Any, _sender: str, _subject: str) -> Json:
    """TODO: report spam to Spamhaus."""
    return {}


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take one spam argument."""
        parser.add_argument('spam', help='sender address, then optionally a space and subject')

    def handle(self, *_args: object, spam: str, **_options: object) -> None:
        """Smoke test IMAP and Spamhaus connectivity."""
        sender, _, subject = spam.partition(' ')
        logger.info('Hammering %s %r', sender, subject)
        logger.info(
            'Cancel: %s',
            f'gh run cancel {run_id} --repo {environ["GITHUB_REPOSITORY"]}'
            if (run_id := environ.get('GITHUB_RUN_ID'))
            else f'kill {getpid()}',
        )
        # TODO: delay so the workflow can be cancelled (undo) before acting
        run(delete_and_filter(sender))
        run(report(sender, subject))
