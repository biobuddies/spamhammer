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

from hammer.management.commands.junk_email import junk_email, match_messages


def make_imap(domain: str = 'news.example.com') -> AsyncMock:
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
                bytearray(f'From: Sender <sender@{domain}>\r\n'.encode()),
                b')',
                b'2 FETCH (BODY[HEADER] {20}',
                bytearray(f'From: other@{domain}\r\n'.encode()),
                b' UID 22)',
                b'3 FETCH (UID 33 BODY[HEADER] {20}',
                bytearray(b'From: sender@notnews.example.com\r\n'),
                b')',
            ],
        ),
        Response('OK', []),
        Response('OK', []),
    ]
    return imap


@mark.parametrize(
    ('domain', 'sequence', 'count'), (('example.com', '11', 1), ('news.example.com', '11,22', 2))
)
@mark.parametrize('dry_run', (True, False))
def test_junk_scope(domain: str, sequence: str, count: int, dry_run: bool) -> None:
    imap = make_imap(domain)
    counts = run(unwrap(junk_email)(imap, f'sender@{domain}', dry_run=dry_run))
    assert counts.total() == count
    assert imap.uid.await_count == (1 if dry_run else 3)
    if dry_run:
        imap.status.assert_not_awaited()
    else:
        assert imap.uid.await_args_list[1].args == ('STORE', sequence, '+FLAGS.SILENT', '(\\Seen)')
        assert imap.uid.await_args_list[2].args == ('MOVE', sequence, '"Junk"')


@mark.parametrize('failure', ('destination', 'capability', 'store', 'move'))
def test_junk_failure(failure: str) -> None:
    imap = make_imap()
    if failure == 'destination':
        imap.status.return_value = Response('NO', [])
    if failure == 'capability':
        imap.protocol.capabilities = set()
    responses = list(imap.uid.side_effect)
    if failure in {'store', 'move'}:
        responses[1 if failure == 'store' else 2] = Response('NO', [])
    imap.uid.side_effect = responses
    with raises(CommandError):
        run(unwrap(junk_email)(imap, 'sender@news.example.com', dry_run=False))
    assert (
        imap.uid.await_count == {'destination': 1, 'capability': 1, 'store': 2, 'move': 3}[failure]
    )


def test_junk_empty() -> None:
    imap = make_imap()
    imap.uid_search.return_value = Response('OK', [b''])
    assert run(unwrap(junk_email)(imap, 'sender@news.example.com', dry_run=False)) == Counter()
    imap.uid.assert_not_awaited()


def test_junk_missing_uid() -> None:
    with raises(CommandError, match='missing UID'):
        match_messages([bytearray(b'From: sender@example.com\r\n'), b')'], 'sender@example.com')


@mark.parametrize('dry_run', (True, False))
@mark.parametrize('domain', ('one.domain', 'enews.united.com'))
def test_junk_output(domain: str, dry_run: bool) -> None:
    output = StringIO()
    counts = (
        Counter({f'Sender@{domain}': 2, f'Other@{domain}': 1})
        if domain.count('.') >= 2
        else Counter({f'Sender@{domain}': 3})
    )
    with patch(
        'hammer.management.commands.junk_email.junk_email', new=AsyncMock(return_value=counts)
    ):
        call_command(
            'junk_email', f'Sender@{domain}', dry_run=dry_run, folder='Junk', stdout=output
        )
    scope = domain if domain.count('.') >= 2 else f'Sender@{domain}'
    suffix = ': Sender@, Other@' if domain.count('.') >= 2 else ''
    assert (
        output.getvalue()
        == f'{"Would move" if dry_run else "Moved"} 3 messages from {scope} to Junk{suffix}\n'
    )


def test_junk_excludes_deeper_domains() -> None:
    lines = [
        b'1 FETCH (UID 11 BODY[HEADER] {20}',
        bytearray(b'From: sender@deeper.news.example.com\r\n'),
        b')',
    ]
    assert match_messages(lines, 'news.example.com') == {}
