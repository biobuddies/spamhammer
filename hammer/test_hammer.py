from asyncio import CancelledError, run
from inspect import unwrap
from logging import DEBUG, INFO
from os import getpid
from unittest.mock import AsyncMock

from aiohttp import ClientResponseError, ClientSession, web
from aiohttp.test_utils import TestServer
from aioimaplib import Response
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer.management.commands import hammer
from hammer.management.commands.hammer import Json, fetch_spam, json_session, report


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
    monkeypatch.setenv('GITHUB_REPOSITORY', 'biobuddies/spamhammer')
    monkeypatch.setenv('GITHUB_RUN_ID', '42')
    with caplog.at_level(INFO):
        call_command('hammer', 'spam@example.com')
    assert 'Cancel: gh run cancel 42 --repo biobuddies/spamhammer' in caplog.text


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

    async def list_submissions(_request: web.Request) -> web.Response:
        return web.Response(text='Ready')

    application.router.add_get('/submissions/list', list_submissions)

    async def check_session() -> None:
        async with TestServer(application) as server:

            @json_session('TEST_API_KEY', str(server.make_url('/')), 'submissions/list')
            async def inner(_session: ClientSession) -> Json:
                return {}

            await inner()

    with caplog.at_level(DEBUG):
        run(check_session())
    assert [
        record.levelno
        for record in caplog.records
        if record.name.startswith('hammer') and '/submissions/list' in record.getMessage()
    ] == [INFO]


def test_report_submits_raw_message(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    submissions = []

    async def list_submissions(_request: web.Request) -> web.Response:
        return web.json_response([])

    async def add_email(request: web.Request) -> web.Response:
        submissions.append(await request.json())
        return web.json_response({'id': 'abc'})

    application = web.Application()
    application.router.add_get('/submissions/list', list_submissions)
    application.router.add_post('/submissions/add/email', add_email)

    async def submit() -> None:
        async with TestServer(application) as server:
            await json_session('SPAMHAUS_API_KEY', str(server.make_url('/')), 'submissions/list')(
                unwrap(report)
            )('Unsolicited email from spam@example.com', b'From: spam@example.com\r\n\r\nAct now')

    monkeypatch.setenv('SPAMHAUS_API_KEY', 'key')
    with caplog.at_level(INFO):
        run(submit())
    assert submissions == [
        {
            'reason': 'Unsolicited email from spam@example.com',
            'source': {'object': 'From: spam@example.com\r\n\r\nAct now'},
            'threat_type': 'spam',
        }
    ]
    assert 'Spamhaus abc' in caplog.text


@mark.parametrize('include_match', (True, False))
def test_fetch_spam_excludes_similar_senders(include_match: bool) -> None:
    expected = bytearray(b'From: Sender <sender@example.com>\r\n\r\nBody')
    other = bytearray(b'From: notsender@example.com\r\n\r\nOther')
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.uid_search.return_value = Response('OK', [b'1 2'])
    imap.uid.return_value = Response('OK', [expected, other] if include_match else [other])
    assert run(unwrap(fetch_spam)(imap, 'sender@example.com', '')) == (
        expected if include_match else None
    )
    imap.uid.assert_awaited_once_with('FETCH', '1,2', 'BODY.PEEK[]')


@mark.parametrize('message', (None, b'From: sender@example.com\r\n\r\nBody'))
def test_report_waits_only_for_matches(message: bytes | None, monkeypatch: MonkeyPatch) -> None:
    events = []
    monkeypatch.setattr(hammer, 'fetch_spam', fetch := AsyncMock(return_value=message))
    monkeypatch.setattr(
        hammer, 'sleep', wait := AsyncMock(side_effect=lambda _seconds: events.append('wait'))
    )
    monkeypatch.setattr(
        hammer, 'report', report := AsyncMock(side_effect=lambda *_args: events.append('report'))
    )
    assert run(hammer.report_after_delay('sender@example.com', 'Offer', 'Spam', 7)) == message
    fetch.assert_awaited_once_with('sender@example.com', 'Offer')
    report.assert_awaited_once_with('Spam', message)
    assert events == (['wait', 'report'] if message else ['report'])
    if message:
        wait.assert_awaited_once_with(7)
    else:
        wait.assert_not_awaited()


def test_cancelled_delay_does_not_report(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(hammer, 'fetch_spam', AsyncMock(return_value=b'email'))
    monkeypatch.setattr(hammer, 'sleep', AsyncMock(side_effect=CancelledError))
    monkeypatch.setattr(hammer, 'report', report := AsyncMock())
    with raises(CancelledError):
        run(hammer.report_after_delay('sender@example.com', '', 'Spam', 7))
    report.assert_not_awaited()


@mark.parametrize(('check_status', 'submit_status'), ((401, 200), (200, 400)))
def test_report_propagates_http_failures(
    caplog: LogCaptureFixture, check_status: int, submit_status: int, monkeypatch: MonkeyPatch
) -> None:
    requests = []

    async def list_submissions(request: web.Request) -> web.Response:
        assert request.headers['Authorization'] == 'Bearer key'
        requests.append('check')
        return web.Response(status=check_status)

    async def add_email(_request: web.Request) -> web.Response:
        requests.append('submit')
        return web.json_response({'message': 'unsupported threat type'}, status=submit_status)

    application = web.Application()
    application.router.add_get('/submissions/list', list_submissions)
    application.router.add_post('/submissions/add/email', add_email)

    async def submit() -> None:
        async with TestServer(application) as server:
            await json_session('TEST_API_KEY', str(server.make_url('/')), 'submissions/list')(
                unwrap(report)
            )('Spam', b'From: sender@example.com\r\n\r\nBody')

    monkeypatch.setenv('TEST_API_KEY', 'key')
    with caplog.at_level(INFO), raises(ClientResponseError) as error:
        run(submit())
    assert error.value.status == (check_status if check_status != 200 else submit_status)
    assert requests == (['check'] if check_status != 200 else ['check', 'submit'])

    assert [
        record.getMessage().split(' st=')[1].split()[0]
        for record in caplog.records
        if record.name == hammer.__name__ and ' st=' in record.getMessage()
    ] == ([str(check_status)] if check_status != 200 else ['200', str(submit_status)])


def test_report_rejects_negative_delay(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(hammer, 'fetch_spam', fetch := AsyncMock())
    with raises(CommandError, match='nonnegative'):
        run(hammer.report_after_delay('sender@example.com', '', 'Spam', -1))
    fetch.assert_not_awaited()


@mark.parametrize('operation', ('select', 'uid_search', 'uid'))
def test_fetch_spam_rejects_imap_failures(operation: str) -> None:
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.uid_search.return_value = Response('OK', [b'1'])
    getattr(imap, operation).return_value = Response('NO', [b'Unavailable'])
    with raises(CommandError, match='NO'):
        run(unwrap(fetch_spam)(imap, 'sender@example.com', ''))
