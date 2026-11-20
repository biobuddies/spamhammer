"""Connect to mail servers and report spam to Spamhaus."""

from asyncio import StreamReader, StreamWriter, get_running_loop, open_connection, sleep
from base64 import b64encode
from collections.abc import Callable, Coroutine
from email import message_from_bytes, policy
from email.utils import parseaddr
from functools import wraps
from logging import getLogger
from os import environ
from re import finditer, split
from ssl import create_default_context
from types import SimpleNamespace
from typing import Any, Concatenate
from urllib.parse import ParseResult, unquote, urlparse

from aiohttp import (
    ClientSession,
    ClientTimeout,
    TraceConfig,
    TraceRequestEndParams,
    TraceRequestExceptionParams,
    TraceRequestStartParams,
)
from aioimaplib import IMAP4_SSL, Response
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

logger = getLogger(__name__)
type Json = dict[str, Any]
type JsonSession[**P] = Callable[Concatenate[ClientSession, P], Coroutine[Any, Any, Json]]
type Runner[**P, T] = Callable[P, Coroutine[Any, Any, T | None]]
type ImapSession[**P, T] = Callable[Concatenate[IMAP4_SSL, P], Coroutine[Any, Any, T]]
type SieveSession[**P, T] = Callable[Concatenate['ManageSieve', P], Coroutine[Any, Any, T]]


class ImapClient(IMAP4_SSL):
    """Raise on failed login and mailbox commands."""

    def __getattribute__(self, name: str) -> Any:
        """Check the response from selected inherited commands."""
        method = super().__getattribute__(name)
        if name not in {'fetch', 'login', 'search', 'select', 'status', 'uid', 'uid_search'}:
            return method

        @wraps(method)
        async def inner(*args: str, **kwargs: str | None) -> Response:
            response = await method(*args, **kwargs)
            if response.result == 'OK':
                return response
            match name:
                case 'select':
                    mailbox = args[0] if args else kwargs.get('mailbox', 'INBOX')
                    operation = f'{mailbox} SELECT'
                case 'uid':
                    verb = args[0] if args else kwargs['command']
                    operation = f'UID {verb}'
                case _:
                    operation = name.replace('_', ' ').upper()
            raise CommandError(f'{self.host} {operation} {response.result}')

        return inner


def read_email_url() -> ParseResult | None:
    """Parse SPAMHAMMER_EMAIL_URLS, delimited by commas and/or whitespace; None when off."""
    match [url for url in split(r'[\s,]+', environ.get('SPAMHAMMER_EMAIL_URLS', 'off')) if url]:
        case []:
            raise ImproperlyConfigured('SPAMHAMMER_EMAIL_URLS empty; off skips')
        case [url] if url.startswith('off'):
            logger.info('SPAMHAMMER_EMAIL_URLS off')
            return None
        case [url]:
            return urlparse(url)
        case _:
            raise NotImplementedError('exactly one SPAMHAMMER_EMAIL_URLS supported for now')


def imap_session[**P, T](
    off: T | None = None, *, requires: tuple[str, ...] = ()
) -> Callable[[ImapSession[P, T]], Runner[P, T]]:
    """Supply a client logged in to SPAMHAMMER_EMAIL_URLS, smoke tested by LOGIN.

    URLs, delimited by commas and/or whitespace, starting with 'off' return `off` instead.
    Check required capabilities immediately after login.
    """

    def decorate(function: ImapSession[P, T]) -> Runner[P, T]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T | None:
            if not (parsed := read_email_url()):
                return off
            imap = ImapClient(parsed.hostname or '', parsed.port or 993)
            await imap.wait_hello_from_server()
            await imap.login(unquote(parsed.username or ''), unquote(parsed.password or ''))
            logger.info('%s LOGIN OK', parsed.hostname)
            try:
                capabilities = imap.protocol.capabilities if imap.protocol else ()
                if missing := set(requires).difference(capabilities):
                    raise CommandError(f'Missing IMAP capabilities: {", ".join(sorted(missing))}')
                return await function(imap, *args, **kwargs)
            finally:
                await imap.logout()

        return inner

    return decorate


class ManageSieve:
    """Commands over one stream; NO and BYE raise ConnectionError."""

    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def read_response(self, verb: str) -> list[list[str]]:
        """Return tokenized lines before OK, splicing in announced literals."""
        lines = []
        tokens: list[str] = []
        while True:
            line = (await self.reader.readline()).decode()
            if not line:
                raise ConnectionError(f'ManageSieve {verb} connection closed')
            size = None
            for match in finditer(r'"((?:[^"\\]|\\.)*)"|\{(\d+)\+?\}\r?$|(\S+)', line):
                quoted, size_text, atom = match.groups()
                if size_text:
                    size = int(size_text)
                else:
                    tokens.append(atom or quoted.replace('\\"', '"').replace('\\\\', '\\'))
            if size is not None:
                # Scripts travel with CRLF line endings; Python compares and edits with LF
                tokens.append((await self.reader.readexactly(size)).decode().replace('\r\n', '\n'))
                continue
            if tokens[:1] == ['OK']:
                return lines
            if tokens[:1] in (['NO'], ['BYE']):
                raise ConnectionError(f'ManageSieve {verb} {" ".join(tokens)}')
            lines.append(tokens)
            tokens = []

    def __getattr__(self, verb: str) -> Callable[..., Coroutine[Any, Any, list[list[str]]]]:
        """Send an RFC 5804 command, e.g. `await sieve.getscript('"name"')`."""
        if verb not in {
            'authenticate',
            'checkscript',
            'getscript',
            'listscripts',
            'logout',
            'putscript',
            'setactive',
            'starttls',
        }:
            raise AttributeError(verb)

        async def inner(*args: str) -> list[list[str]]:
            self.writer.write(f'{" ".join((verb.upper(), *args))}\r\n'.encode())
            await self.writer.drain()
            return await self.read_response(verb.upper())

        return inner


def sieve_session[**P, T](off: T | None = None) -> Callable[[SieveSession[P, T]], Runner[P, T]]:
    """Supply a client logged in with SPAMHAMMER_EMAIL_URLS over STARTTLS; `off` when off."""

    def decorate(function: SieveSession[P, T]) -> Runner[P, T]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T | None:
            if not (parsed := read_email_url()):
                return off
            hostname = parsed.hostname or ''
            sieve = ManageSieve(*await open_connection(hostname, 4190))
            await sieve.read_response('greeting')
            await sieve.starttls()
            await sieve.writer.start_tls(create_default_context(), server_hostname=hostname)
            await sieve.read_response('capabilities')
            await sieve.authenticate(
                '"PLAIN"',
                '"%s"'
                % b64encode(
                    f'\0{unquote(parsed.username or "")}\0{unquote(parsed.password or "")}'.encode()
                ).decode(),
            )
            logger.info('%s ManageSieve AUTHENTICATE OK', hostname)
            try:
                return await function(sieve, *args, **kwargs)
            finally:
                await sieve.logout()
                sieve.writer.close()

        return inner

    return decorate


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
                raise ImproperlyConfigured(f'{variable} empty; off skips')
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
    await imap.select('INBOX')
    criteria = ('FROM', f'"{sender}"', *(('SUBJECT', f'"{subject}"') if subject else ()))
    found = await imap.uid_search(*criteria)
    numbers = found.lines[0].decode().split()
    if not numbers:
        return None
    fetched = await imap.uid('FETCH', ','.join(numbers), 'BODY.PEEK[]')
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
async def report(session: ClientSession, sender: str, message: bytes | None) -> Json:
    """Submit the raw message, if any, to Spamhaus as spam; 208 means already reported."""
    if message is None:
        logger.info('No INBOX message matches')
        return {}
    async with session.post(
        'submissions/add/email',
        json={
            'reason': f'Unsolicited email from {sender}',
            'source': {'object': message.decode(errors='replace')},
            'threat_type': 'spam',
        },
    ) as response:
        submission = await response.json()
    logger.info('Spamhaus %s', submission.get('id', response.status))
    return submission


async def report_after_delay(
    sender: str, subject: str, delay_seconds: int
) -> tuple[bytes | None, Json]:
    """Wait asynchronously before reporting a matching message; return it and the submission."""
    message = await fetch_spam(sender, subject)
    if message:
        logger.info('Reporting in %d seconds', delay_seconds)
        await sleep(delay_seconds)
    return message, await report(sender, message)
