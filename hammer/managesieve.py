"""Minimal asyncio ManageSieve (RFC 5804) client, STARTTLS and SASL PLAIN only."""

from asyncio import StreamReader, StreamWriter, open_connection
from base64 import b64encode
from collections.abc import Callable, Coroutine
from functools import wraps
from logging import getLogger
from re import finditer
from ssl import create_default_context
from typing import Any, Concatenate
from urllib.parse import unquote

from hammer.management.commands.hammer import quote
from hammer.management.commands.list_email import read_email_url

logger = getLogger(__name__)
type Runner[**P, T] = Callable[P, Coroutine[Any, Any, T | None]]
type Session[**P, T] = Callable[Concatenate['ManageSieve', P], Coroutine[Any, Any, T]]


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
                tokens.append((await self.reader.readexactly(size)).decode())
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

        async def command(*arguments: str) -> list[list[str]]:
            self.writer.write(f'{" ".join((verb.upper(), *arguments))}\r\n'.encode())
            await self.writer.drain()
            return await self.read_response(verb.upper())

        return command


def sieve_session[**P, T](off: T | None = None) -> Callable[[Session[P, T]], Runner[P, T]]:
    """Supply a client logged in with SPAMHAMMER_EMAIL_URLS over STARTTLS; `off` when off."""

    def decorate(function: Session[P, T]) -> Runner[P, T]:
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
            credentials = f'\0{unquote(parsed.username or "")}\0{unquote(parsed.password or "")}'
            await sieve.authenticate('"PLAIN"', quote(b64encode(credentials.encode()).decode()))
            logger.info('%s ManageSieve AUTHENTICATE OK', hostname)
            try:
                return await function(sieve, *args, **kwargs)
            finally:
                await sieve.logout()
                sieve.writer.close()

        return inner

    return decorate
