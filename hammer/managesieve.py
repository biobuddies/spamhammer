"""Minimal asyncio ManageSieve (RFC 5804) client, STARTTLS and SASL PLAIN only."""

from asyncio import StreamReader, StreamWriter, open_connection
from base64 import b64encode
from collections.abc import Callable, Coroutine
from functools import wraps
from logging import getLogger
from re import finditer
from ssl import create_default_context
from typing import Any, Concatenate
from urllib.parse import ParseResult, unquote

from hammer.management.commands.hammer import quote
from hammer.management.commands.list_email import read_email_url

logger = getLogger(__name__)
type Runner[**P, T] = Callable[P, Coroutine[Any, Any, T | None]]
type Session[**P, T] = Callable[Concatenate['ManageSieve', P], Coroutine[Any, Any, T]]


def literal(text: str) -> str:
    """Encode a non-synchronizing literal, which RFC 5804 servers must accept."""
    return f'{{{len(text.encode())}+}}\r\n{text}'


class ManageSieve:
    """Commands over one authenticated stream; NO and BYE raise ConnectionError."""

    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    async def read_tokens(self) -> list[str]:
        """Read one response line, splicing in any literals it announces."""
        tokens = []
        while True:
            line = (await self.reader.readline()).decode()
            if not line:
                raise ConnectionError('ManageSieve connection closed')
            size = None
            for match in finditer(r'"((?:[^"\\]|\\.)*)"|\{(\d+)\+?\}\r?$|(\S+)', line):
                quoted, size_text, atom = match.groups()
                if size_text:
                    size = int(size_text)
                else:
                    tokens.append(atom or quoted.replace('\\"', '"').replace('\\\\', '\\'))
            if size is None:
                return tokens
            tokens.append((await self.reader.readexactly(size)).decode())

    async def read_response(self, verb: str) -> list[list[str]]:
        """Return data lines before OK; raise on NO or BYE."""
        lines = []
        while (tokens := await self.read_tokens())[0] not in {'OK', 'NO', 'BYE'}:
            lines.append(tokens)
        if tokens[0] != 'OK':
            raise ConnectionError(f'ManageSieve {verb} {" ".join(tokens)}')
        return lines

    async def command(self, verb: str, *arguments: str) -> list[list[str]]:
        """Send one command line and await its response."""
        self.writer.write(' '.join((verb, *arguments)).encode() + b'\r\n')
        await self.writer.drain()
        return await self.read_response(verb)

    async def list_scripts(self) -> tuple[str | None, list[str]]:
        """Return the active script name, if any, and the inactive names."""
        lines = await self.command('LISTSCRIPTS')
        active = next((name for name, *flags in lines if flags == ['ACTIVE']), None)
        return active, [name for name, *flags in lines if not flags]

    async def get_script(self, name: str) -> str:
        """GETSCRIPT, RFC 5804 section 2.9."""
        return (await self.command('GETSCRIPT', quote(name)))[0][0]

    async def check_script(self, script: str) -> None:
        """CHECKSCRIPT, RFC 5804 section 2.12; raises on errors."""
        await self.command('CHECKSCRIPT', literal(script))

    async def put_script(self, name: str, script: str) -> None:
        """PUTSCRIPT, RFC 5804 section 2.6, replacing the whole script."""
        await self.command('PUTSCRIPT', quote(name), literal(script))

    async def set_active(self, name: str) -> None:
        """SETACTIVE, RFC 5804 section 2.8."""
        await self.command('SETACTIVE', quote(name))


async def connect(parsed: ParseResult) -> ManageSieve:
    """Connect to port 4190, upgrade with STARTTLS, and log in with SASL PLAIN."""
    hostname = parsed.hostname or ''
    sieve = ManageSieve(*await open_connection(hostname, 4190))
    await sieve.read_response('greeting')
    await sieve.command('STARTTLS')
    await sieve.writer.start_tls(create_default_context(), server_hostname=hostname)
    await sieve.read_response('capabilities')
    credentials = f'\0{unquote(parsed.username or "")}\0{unquote(parsed.password or "")}'
    await sieve.command('AUTHENTICATE', '"PLAIN"', quote(b64encode(credentials.encode()).decode()))
    logger.info('%s ManageSieve AUTHENTICATE OK', hostname)
    return sieve


def sieve_session[**P, T](off: T | None = None) -> Callable[[Session[P, T]], Runner[P, T]]:
    """Supply a ManageSieve client logged in with SPAMHAMMER_EMAIL_URLS; `off` when off."""

    def decorate(function: Session[P, T]) -> Runner[P, T]:
        @wraps(function)
        async def inner(*args: P.args, **kwargs: P.kwargs) -> T | None:
            if not (parsed := read_email_url()):
                return off
            sieve = await connect(parsed)
            try:
                return await function(sieve, *args, **kwargs)
            finally:
                await sieve.command('LOGOUT')
                sieve.writer.close()

        return inner

    return decorate
