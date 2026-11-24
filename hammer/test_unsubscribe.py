"""Exercise one-click header parsing and HTTP requests without contacting a sender."""

from asyncio import run
from pathlib import Path
from re import findall
from unittest.mock import AsyncMock, patch

from aiohttp import web
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import CaptureFixture, mark, raises

from hammer.management.commands.unsubscribe import find_unsubscribe_url, unsubscribe


@mark.parametrize(
    'headers',
    (
        '',
        'List-Unsubscribe: <https://optout.spoton.com/a>, <https://optout.spoton.com/b>\r\n',
        'List-Unsubscribe: <https://user:password@optout.spoton.com/leave>\r\n',
        'List-Unsubscribe: <https://:password@optout.spoton.com/leave>\r\n',
        'List-Unsubscribe: <https://@optout.spoton.com/leave>\r\n',
        'List-Unsubscribe: <https://optout.spoton.com/leave#fragment>\r\n',
        'List-Unsubscribe: <https://optout.spoton.com/leave>\r\n' * 2,
    ),
)
def test_reject_unsupported_headers(headers: str) -> None:
    with raises(CommandError):
        find_unsubscribe_url(
            (headers + 'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n').encode()
        )


HAMMER = Path(__file__).parent


@mark.parametrize(
    'path',
    sorted((
        *(HAMMER / 'baloney').glob('*.eml'),
        *(HAMMER / 'spam' / 'yes-one-click').glob('*.eml'),
    )),
    ids=lambda path: path.stem,
)
def test_find_unsubscribe_url(path: Path) -> None:
    raw = path.read_bytes()
    assert find_unsubscribe_url(raw) in findall(r'<(https?://[^<>]+)>', raw.decode())


@mark.parametrize(
    'path', sorted((HAMMER / 'spam' / 'not-one-click').glob('*.eml')), ids=lambda path: path.stem
)
def test_reject_real_headers(path: Path) -> None:
    with raises(CommandError):
        find_unsubscribe_url(path.read_bytes())


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
                        await unsubscribe('campaigns@spoton.marketing', '', dry_run=False) or ''
                    )
                else:
                    with raises(CommandError, match=f'HTTP {status}'):
                        await unsubscribe('campaigns@spoton.marketing', '', dry_run=False)
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


def test_unsubscribe_dry_run(capsys: CaptureFixture[str]) -> None:
    raw = (HAMMER / 'baloney' / 'goorsha.eml').read_bytes()
    with (
        patch(
            'hammer.management.commands.unsubscribe.fetch_email', new=AsyncMock(return_value=raw)
        ) as fetch,
        patch('hammer.management.commands.unsubscribe.ClientSession') as session,
    ):
        call_command('unsubscribe', 'campaigns@spoton.marketing Goorsha', dry_run=True)
    fetch.assert_awaited_once_with('campaigns@spoton.marketing', 'Goorsha')
    session.assert_not_called()
    assert capsys.readouterr().out == (
        'Would request unsubscribe for campaigns@spoton.marketing via optout.spoton.com\n'
        'Would move 0 messages from campaigns@spoton.marketing to Trash\n'
    )


@mark.parametrize('accepted', (True, False))
def test_unsubscribe_sweeps_regardless_of_result(accepted: bool) -> None:
    with (
        patch(
            'hammer.management.commands.unsubscribe.unsubscribe',
            new=(
                AsyncMock(return_value='Accepted')
                if accepted
                else AsyncMock(side_effect=CommandError('Failed'))
            ),
        ),
        patch(
            'hammer.management.commands.sweep.sweep', new=AsyncMock(return_value=None)
        ) as cleanup,
    ):
        if accepted:
            call_command('unsubscribe', 'campaigns@spoton.marketing Goorsha')
        else:
            with raises(CommandError, match='Failed'):
                call_command('unsubscribe', 'campaigns@spoton.marketing Goorsha')
    cleanup.assert_awaited_once_with('campaigns@spoton.marketing', dry_run=False, folder='Trash')
