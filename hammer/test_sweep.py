"""Exercise exact cleanup scope and fail-closed IMAP mutations."""

from asyncio import run
from collections import Counter
from inspect import unwrap
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aioimaplib import Response
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import mark, raises

from hammer.management.commands.sweep import match_messages, sweep


def make_imap(domain: str = 'news.unchartedvalues.com') -> AsyncMock:
    imap = AsyncMock()
    imap.protocol = SimpleNamespace(capabilities={'MOVE'})
    imap.select.return_value = Response('OK', [])
    imap.status.return_value = Response('OK', [])
    imap.uid_search.return_value = Response('OK', [b'11 22 33'])
    imap.uid.side_effect = [
        Response(
            'OK',
            [
                b'1 FETCH (UID 11 BODY[HEADER] {20}',
                bytearray(f'From: <contact@{domain}>\r\n'.encode()),
                b')',
                b'2 FETCH (BODY[HEADER] {20}',
                bytearray(f'From: other@{domain}\r\n'.encode()),
                b' UID 22)',
                b'3 FETCH (UID 33 BODY[HEADER] {20}',
                bytearray(b'From: contact@notnews.unchartedvalues.com\r\n'),
                b')',
            ],
        ),
        Response('OK', []),
    ]
    return imap


@mark.parametrize(
    ('domain', 'sequence', 'count'),
    (('unchartedvalues.com', '11', 1), ('news.unchartedvalues.com', '11,22', 2)),
)
@mark.parametrize('dry_run', (True, False))
def test_sweep_scope(domain: str, sequence: str, count: int, dry_run: bool) -> None:
    imap = make_imap(domain)
    counts = run(unwrap(sweep)(imap, f'contact@{domain}', dry_run=dry_run))
    assert counts.total() == count
    assert imap.uid.await_count == (1 if dry_run else 2)
    if dry_run:
        imap.status.assert_not_awaited()
    else:
        imap.status.assert_awaited_once_with('"Spam"', '(UIDVALIDITY)')
        assert imap.uid.await_args_list[1].args == ('MOVE', sequence, '"Spam"')


@mark.parametrize('failure', ('destination', 'capability', 'move'))
def test_sweep_failure(failure: str) -> None:
    imap = make_imap()
    if failure == 'destination':
        imap.status.return_value = Response('NO', [])
    if failure == 'capability':
        imap.protocol.capabilities = set()
    responses = list(imap.uid.side_effect)
    if failure == 'move':
        responses[1] = CommandError('UID MOVE NO')
    imap.uid.side_effect = responses
    with raises(CommandError):
        run(unwrap(sweep)(imap, 'contact@news.unchartedvalues.com', dry_run=False))
    assert imap.uid.await_count == (2 if failure == 'move' else 1)


def test_sweep_empty() -> None:
    imap = make_imap()
    imap.uid_search.return_value = Response('OK', [b''])
    assert run(unwrap(sweep)(imap, 'contact@news.unchartedvalues.com', dry_run=False)) == Counter()
    imap.uid.assert_not_awaited()


def test_sweep_missing_uid() -> None:
    with raises(CommandError, match='missing UID'):
        match_messages(
            [bytearray(b'From: contact@unchartedvalues.com\r\n'), b')'],
            'contact@unchartedvalues.com',
        )


@mark.parametrize('dry_run', (True, False))
@mark.parametrize('domain', ('thealphapaths.com', 'news.meadowbrookupdates.com'))
def test_sweep_output(domain: str, dry_run: bool) -> None:
    output = StringIO()
    counts = (
        Counter({f'Sender@{domain}': 2, f'Other@{domain}': 1})
        if domain.count('.') >= 2
        else Counter({f'Sender@{domain}': 3})
    )
    with patch('hammer.management.commands.sweep.sweep', new=AsyncMock(return_value=counts)):
        call_command('sweep', f'Sender@{domain}', dry_run=dry_run, stdout=output)
    scope = domain if domain.count('.') >= 2 else f'Sender@{domain}'
    suffix = ': Sender@, Other@' if domain.count('.') >= 2 else ''
    assert (
        output.getvalue()
        == f'{"Would move" if dry_run else "Moved"} 3 messages from {scope} to Spam{suffix}\n'
    )


def test_sweep_excludes_deeper_domains() -> None:
    lines = [
        b'1 FETCH (UID 11 BODY[HEADER] {20}',
        bytearray(b'From: contact@deeper.news.unchartedvalues.com\r\n'),
        b')',
    ]
    assert match_messages(lines, 'news.unchartedvalues.com') == {}
