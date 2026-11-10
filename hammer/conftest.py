"""Fake a ManageSieve server from canned responses."""

from asyncio import StreamReader
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

from pytest import MonkeyPatch, fixture

from hammer import managesieve
from hammer.managesieve import ManageSieve


@fixture
def sieve_server(monkeypatch: MonkeyPatch) -> Callable[..., Callable[[], bytes]]:
    """Answer ManageSieve commands with canned responses; return a reader of what was sent."""

    def serve(*responses: bytes) -> Callable[[], bytes]:
        monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://buddy:secret@imap.biobuddi.es')
        writer = MagicMock(drain=AsyncMock())

        async def connect(_parsed: object) -> ManageSieve:
            reader = StreamReader()
            reader.feed_data(b''.join(responses))
            reader.feed_eof()
            return ManageSieve(reader, writer)

        monkeypatch.setattr(managesieve, 'connect', connect)
        return lambda: b''.join(call.args[0] for call in writer.write.call_args_list)

    return serve
