"""Report spam to Spamhaus."""

from asyncio import get_running_loop, run, sleep
from collections.abc import Callable, Coroutine
from email import message_from_bytes, policy
from email.utils import parseaddr, quote
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
    TraceRequestExceptionParams,
    TraceRequestStartParams,
)
from aioimaplib import IMAP4_SSL
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser

from hammer.management.commands.list_email import imap_session

logger = getLogger(__name__)
type Json = dict[str, Any]
type JsonSession[**P] = Callable[Concatenate[ClientSession, P], Coroutine[Any, Any, Json]]


async def time_request(
    _session: ClientSession, context: SimpleNamespace, _params: TraceRequestStartParams
) -> None:
    context.start = get_running_loop().time()


async def log_request(
    _session: ClientSession,
    context: SimpleNamespace,
    params: TraceRequestEndParams | TraceRequestExceptionParams,
) -> None:
    logger.info(
        '%s %s st=%s rt=%.3fs',
        params.method,
        params.url,
        params.response.status
        if isinstance(params, TraceRequestEndParams)
        else getattr(params.exception, 'status', 'error'),
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
            trace.on_request_start.append(time_request)
            trace.on_request_end.append(log_request)
            trace.on_request_exception.append(log_request)
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
async def fetch_spam(imap: IMAP4_SSL, sender: str, subject: str) -> bytes | None:
    """Return the latest raw inbox message from sender, and with subject if given, left unread."""
    selected = await imap.select('INBOX')
    if selected.result != 'OK':
        raise CommandError(f'INBOX SELECT {selected.result}')
    criteria = (
        'FROM',
        f'"{quote(sender)}"',
        *(('SUBJECT', f'"{quote(subject)}"') if subject else ()),
    )
    found = await imap.uid_search(*criteria)
    if found.result != 'OK':
        raise CommandError(f'UID SEARCH {found.result}')
    numbers = found.lines[0].decode().split()
    if not numbers:
        return None
    fetched = await imap.uid('FETCH', ','.join(numbers), 'BODY.PEEK[]')
    if fetched.result != 'OK':
        raise CommandError(f'UID FETCH {fetched.result}')
    return next(
        (
            bytes(line)
            for line in reversed(fetched.lines)
            if isinstance(line, bytearray)
            and parseaddr(message_from_bytes(line, policy=policy.default)['From'] or '')[
                1
            ].casefold()
            == sender.casefold()
        ),
        None,
    )


@json_session('SPAMHAUS_API_KEY', 'https://submit.spamhaus.org/portal/api/v1/', 'submissions/list')
async def report(session: ClientSession, reason: str, message: bytes | None) -> Json:
    """Submit the raw message, if any, to Spamhaus as spam; 208 means already reported."""
    if message is None:
        logger.info('No INBOX message matches')
        return {}
    async with session.post(
        'submissions/add/email',
        json={
            'reason': reason,
            'source': {'object': message.decode(errors='replace')},
            'threat_type': 'spam',
        },
    ) as response:
        submission = await response.json()
    logger.info('Spamhaus %s', submission.get('id', response.status))
    return submission


async def report_after_delay(
    sender: str, subject: str, reason: str, delay_seconds: int
) -> bytes | None:
    """Wait asynchronously before reporting a matching message; return it for further reporting."""
    if delay_seconds < 0:
        raise CommandError('--delay-seconds must be nonnegative')
    message = await fetch_spam(sender, subject)
    if message:
        logger.info('Reporting in %d seconds', delay_seconds)
        await sleep(delay_seconds)
    await report(reason, message)
    return message


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser: CommandParser) -> None:
        """Take one spam argument, an optional reason, and a cancellation window."""
        parser.add_argument('spam', help='sender address, then optionally a space and subject')
        parser.add_argument(
            '--delay-seconds', default=60, help='to cancel before reporting', type=int
        )
        parser.add_argument('--reason', help='for Spamhaus; blank or omitted names the sender')

    def handle(
        self, *_args: object, delay_seconds: int, reason: str | None, spam: str, **_options: object
    ) -> None:
        """Report matching inbox messages to Spamhaus."""
        sender, _, subject = spam.partition(' ')
        logger.info('Hammering %s %r', sender, subject)
        logger.info(
            'Cancel: %s',
            f'gh run cancel {run_id} --repo {environ["GITHUB_REPOSITORY"]}'
            if (run_id := environ.get('GITHUB_RUN_ID'))
            else f'kill {getpid()}',
        )
        run(
            report_after_delay(
                sender, subject, reason or f'Unsolicited email from {sender}', delay_seconds
            )
        )
