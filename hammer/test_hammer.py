"""Exercise hammer's graceful degradation without network access."""

from asyncio import run
from logging import DEBUG, INFO
from os import getpid

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer.management.commands.hammer import json_session


@mark.parametrize('key', ('off', 'off until I find it'))
def test_hammer_off(caplog: LogCaptureFixture, key: str, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', key)
    with caplog.at_level(INFO):
        call_command('hammer', 'spam@example.com')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text
    assert 'SPAMHAUS_API_KEY off' in caplog.text


@mark.parametrize(
    ('spam', 'logged'),
    (
        ('spam@example.com', "spam@example.com ''"),
        ('spam@example.com Act now', "spam@example.com 'Act now'"),
        ('spam@example.com  Re: Act now ', "spam@example.com ' Re: Act now '"),
    ),
)
def test_hammer_spam(caplog: LogCaptureFixture, logged: str, spam: str) -> None:
    with caplog.at_level(INFO):
        call_command('hammer', spam)
    assert f'Hammering {logged}' in caplog.text


def test_hammer_rejects_empty_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', '')
    with raises(ImproperlyConfigured):
        call_command('hammer', 'spam@example.com')


def test_hammer_logs_cancel_command(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv('GITHUB_REPOSITORY', 'carolinajess/test-spamhammer')
    monkeypatch.setenv('GITHUB_RUN_ID', '42')
    with caplog.at_level(INFO):
        call_command('hammer', 'spam@example.com')
    assert 'Cancel: gh run cancel 42 --repo carolinajess/test-spamhammer' in caplog.text


def test_hammer_logs_kill_command(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('GITHUB_RUN_ID', raising=False)
    with caplog.at_level(INFO):
        call_command('hammer', 'spam@example.com')
    assert f'Cancel: kill {getpid()}' in caplog.text


def test_json_session_logs_each_request_once(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv('TEST_API_KEY', 'key')
    application = web.Application()

    async def list_types(_request: web.Request) -> web.Response:
        return web.json_response([])

    application.router.add_get('/types', list_types)

    async def request_types() -> None:
        async with TestServer(application) as server:

            @json_session('TEST_API_KEY', str(server.make_url('/')), 'types')
            async def inner(_session: ClientSession, _types: object) -> dict:
                return {}

            await inner()

    with caplog.at_level(DEBUG):
        run(request_types())
    assert [
        record.levelno
        for record in caplog.records
        if record.name.startswith('hammer') and '/types' in record.getMessage()
    ] == [INFO]
