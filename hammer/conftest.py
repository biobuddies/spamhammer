"""Fake a ManageSieve server from canned responses."""

from asyncio import StreamReader
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

from pytest import MonkeyPatch, fixture

from hammer import managesieve


@fixture
def sieve_server(monkeypatch: MonkeyPatch) -> Callable[..., Callable[[], bytes]]:
    """Answer ManageSieve commands after login with canned responses; return what was sent."""

    def serve(*responses: bytes) -> Callable[[], bytes]:
        monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://buddy:secret@imap.biobuddi.es')
        writer = MagicMock(drain=AsyncMock(), start_tls=AsyncMock())

        async def open_connection(hostname: str, port: int) -> tuple[StreamReader, MagicMock]:
            assert (hostname, port) == ('imap.biobuddi.es', 4190)
            reader = StreamReader()
            reader.feed_data(
                b'"SASL" ""\r\nOK\r\nOK\r\n"SASL" "PLAIN"\r\nOK\r\nOK\r\n' + b''.join(responses)
            )
            reader.feed_eof()
            return reader, writer

        monkeypatch.setattr(managesieve, 'open_connection', open_connection)

        def sent() -> bytes:
            login, _, commands = b''.join(
                call.args[0] for call in writer.write.call_args_list
            ).partition(b'=="\r\n')
            assert login == b'STARTTLS\r\nAUTHENTICATE "PLAIN" "AGJ1ZGR5AHNlY3JldA'
            writer.start_tls.assert_awaited_once()
            return commands

        return sent

    return serve
