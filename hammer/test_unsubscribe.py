"""Exercise one-click header parsing and HTTP requests without contacting a sender."""

from asyncio import run
from io import StringIO
from unittest.mock import AsyncMock, patch

from aiohttp import web
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import mark, raises

from hammer.management.commands.unsubscribe import find_unsubscribe_url, unsubscribe


@mark.parametrize(
    'headers',
    (
        '',
        'List-Unsubscribe: <http://example.com/leave>\r\n',
        'List-Unsubscribe: <mailto:leave@example.com>\r\n',
        'List-Unsubscribe: <https://example.com/a>, <https://example.com/b>\r\n',
        'List-Unsubscribe: <https://user:password@example.com/leave>\r\n',
        'List-Unsubscribe: <https://:password@example.com/leave>\r\n',
        'List-Unsubscribe: <https://@example.com/leave>\r\n',
        'List-Unsubscribe: <https://example.com/leave#fragment>\r\n',
        'List-Unsubscribe: <https://example.com/leave>\r\n' * 2,
    ),
)
def test_reject_unsupported_headers(headers: str) -> None:
    with raises(CommandError):
        find_unsubscribe_url(
            (headers + 'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n').encode()
        )


def test_find_unsubscribe_url() -> None:
    raw = (
        b'From: MileagePlus Partner <MileagePlus_Partner@enews.united.com>\r\n'
        b'List-Unsubscribe: <mailto:leave@example.com>,\r\n'
        b' <https://click.enews.united.com/subscription_center.aspx?jwt=example>\r\n'
        b'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n\r\nBody'
    )
    assert find_unsubscribe_url(raw) == (
        'https://click.enews.united.com/subscription_center.aspx?jwt=example'
    )


@mark.parametrize('status', (200, 204, 302, 500))
def test_unsubscribe_http(status: int) -> None:
    received = []

    async def receive(request: web.Request) -> web.Response:
        received.append((request.method, dict(await request.post()), dict(request.headers)))
        return web.Response(status=status, headers={'Location': '/redirected'})

    async def exercise() -> None:
        application = web.Application()
        application.router.add_route('*', '/leave', receive)
        runner = web.AppRunner(application)
        await runner.setup()
        try:
            await web.TCPSite(runner, '127.0.0.1', 0).start()
            url = f'http://127.0.0.1:{runner.addresses[0][1]}/leave'
            with (
                patch(
                    'hammer.management.commands.unsubscribe.fetch_email',
                    new=AsyncMock(return_value=b'email'),
                ),
                patch(
                    'hammer.management.commands.unsubscribe.find_unsubscribe_url', return_value=url
                ),
            ):
                if status < 300:
                    assert 'accepted' in (
                        await unsubscribe('sender@example.com', '', dry_run=False) or ''
                    )
                else:
                    with raises(CommandError, match=f'HTTP {status}'):
                        await unsubscribe('sender@example.com', '', dry_run=False)
        finally:
            await runner.cleanup()

    run(exercise())
    assert len(received) == 1
    method, form, headers = received[0]
    assert method == 'POST'
    assert form == {'List-Unsubscribe': 'One-Click'}
    assert 'Authorization' not in headers
    assert 'Cookie' not in headers
    assert headers['Content-Type'] == 'application/x-www-form-urlencoded'


def test_unsubscribe_dry_run() -> None:
    raw = (
        b'List-Unsubscribe: <https://example.com/leave?token=secret>\r\n'
        b'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n'
    )
    output = StringIO()
    with (
        patch(
            'hammer.management.commands.unsubscribe.fetch_email', new=AsyncMock(return_value=raw)
        ) as fetch,
        patch('hammer.management.commands.unsubscribe.ClientSession') as session,
    ):
        call_command('unsubscribe', 'sender@example.com Offer', dry_run=True, stdout=output)
    fetch.assert_awaited_once_with('sender@example.com', 'Offer')
    session.assert_not_called()
    assert output.getvalue() == (
        'Would request unsubscribe for sender@example.com via example.com\n'
        'Would move 0 messages from sender@example.com to Spam\n'
    )


@mark.parametrize('accepted', (True, False))
def test_unsubscribe_cleanup_only_after_success(accepted: bool) -> None:
    request = (
        AsyncMock(return_value='Accepted')
        if accepted
        else AsyncMock(side_effect=CommandError('Failed'))
    )
    with (
        patch('hammer.management.commands.unsubscribe.unsubscribe', new=request),
        patch('hammer.management.commands.unsubscribe.call_command') as cleanup,
    ):
        if accepted:
            call_command('unsubscribe', 'sender@example.com Offer', stdout=StringIO())
        else:
            with raises(CommandError, match='Failed'):
                call_command('unsubscribe', 'sender@example.com Offer')
    assert cleanup.call_count == int(accepted)
    if accepted:
        assert cleanup.call_args.args == ('junk_email', 'sender@example.com')
        assert cleanup.call_args.kwargs['dry_run'] is False
