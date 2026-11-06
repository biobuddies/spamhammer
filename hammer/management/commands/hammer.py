"""Check IMAP and Spamhaus connectivity."""

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
    TraceRequestChunkSentParams,
    TraceRequestEndParams,
    TraceRequestStartParams,
    TraceResponseChunkReceivedParams,
)
from aioimaplib import IMAP4_SSL
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandParser

from hammer.management.commands.list_email import imap_session

logger = getLogger(__name__)
type Json = dict[str, Any]
type JsonSession[**P] = Callable[Concatenate[ClientSession, P], Coroutine[Any, Any, Json]]


async def log_request_start(
    _session: ClientSession, context: SimpleNamespace, params: TraceRequestStartParams
) -> None:
    context.start = get_running_loop().time()
    logger.debug('%s %s', params.method, params.url)


async def log_request_chunk(
    _session: ClientSession, _context: SimpleNamespace, params: TraceRequestChunkSentParams
) -> None:
    logger.debug('%s %s > %s', params.method, params.url, params.chunk)


async def log_response_chunk(
    _session: ClientSession, _context: SimpleNamespace, params: TraceResponseChunkReceivedParams
) -> None:
    logger.debug('%s %s < %s', params.method, params.url, params.chunk)


async def log_request_end(
    _session: ClientSession, context: SimpleNamespace, params: TraceRequestEndParams
) -> None:
    logger.debug(
        '%s %s st=%s rt=%.3fs',
        params.method,
        params.url,
        params.response.status,
        get_running_loop().time() - context.start,
    )


def json_session[**P](
    variable: str, base_url: str, check_path: str
) -> Callable[[JsonSession[P]], Callable[P, Coroutine[Any, Any, Json]]]:
    """Supply an authenticated session after a smoke-test GET.

    Keys starting with 'off', the default, return {} instead.
    """

    def decorate(function: JsonSession[P]) -> Callable[P, Coroutine[Any, Any, Json]]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> Json:
            key = environ.get(variable, 'off')
            if not key:
                raise ImproperlyConfigured(f'{variable} empty; unset or off skips')
            if key.startswith('off'):
                logger.info('%s off', variable)
                return {}
            trace = TraceConfig()
            trace.on_request_start.append(log_request_start)
            trace.on_request_chunk_sent.append(log_request_chunk)
            trace.on_response_chunk_received.append(log_response_chunk)
            trace.on_request_end.append(log_request_end)
            async with ClientSession(
                base_url,
                # Default User-Agents get 403 Forbidden from Spamhaus
                headers={'Authorization': f'Bearer {key}', 'User-Agent': 'spamhammer'},
                raise_for_status=True,
                timeout=ClientTimeout(total=30),
                trace_configs=[trace],
            ) as session:
                async with session.get(check_path) as response:
                    await response.read()
                return await function(session, *args, **kwargs)

        return inner

    return decorate


@imap_session()
async def delete_and_filter(_imap: IMAP4_SSL, _sender: str) -> None:
    """TODO: delete the sender's mail and add a Sieve rule blocking them."""


@json_session('SPAMHAUS_API_KEY', 'https://submit.spamhaus.org/portal/api/v1/', 'submissions/list')
async def report(_session: ClientSession, _sender: str, _subject: str) -> Json:
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
        if run(report(sender, subject)):
            call_command('junk_email', sender, stdout=self.stdout)
