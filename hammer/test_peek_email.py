"""Exercise full-message lookup without a mail server."""

from asyncio import run
from inspect import unwrap
from unittest.mock import AsyncMock, patch

from aioimaplib import Response
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import CaptureFixture, mark, raises

from hammer.management.commands.peek_email import fetch_email


@mark.parametrize('subject', ('', 'Solar is dead.', 'Solar', 'dead', 'is dead'))
def test_fetch_email(subject: str) -> None:
    older = bytearray(
        b'From: contact@news.unchartedvalues.com\r\nSubject: The Truth About Solar\r\n\r\nOld body'
    )
    latest = bytearray(
        'From: <contact@news.unchartedvalues.com>\r\n'
        'Subject: Solar is dead. Here\u2019s what replaces it.\r\n\r\nBody'.encode()
    )
    other = bytearray(
        b'From: contact@news.meadowbrookupdates.com\r\nSubject: Other\r\n\r\nOther body'
    )
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.uid_search.return_value = Response('OK', [b'1 2 3'])
    imap.uid.return_value = Response('OK', [b'FETCH', older, latest, other, b'Done'])
    assert run(unwrap(fetch_email)(imap, 'contact@news.unchartedvalues.com', subject)) == latest
    imap.select.assert_awaited_once_with('INBOX')
    imap.uid.assert_awaited_once_with('FETCH', '1,2,3', 'BODY.PEEK[]')


@mark.parametrize('result', ('NO', 'OK'))
def test_fetch_email_missing_or_failed_search(result: str) -> None:
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.uid_search.return_value = Response('OK', [b''])
    if result == 'NO':
        imap.uid_search.side_effect = CommandError('UID SEARCH NO')
    with raises(CommandError):
        run(unwrap(fetch_email)(imap, 'contact@news.unchartedvalues.com'))
    imap.uid.assert_not_awaited()


def test_peek_email(capsys: CaptureFixture[str]) -> None:
    raw = (
        b'From: contact@news.unchartedvalues.com\r\nSubject: Solar is dead.\r\n\r\n'
        b'Complete body\r\n'
    )
    with patch(
        'hammer.management.commands.peek_email.fetch_email', new=AsyncMock(return_value=raw)
    ):
        call_command('peek_email', 'contact@news.unchartedvalues.com Solar')
    assert capsys.readouterr().out == raw.decode()
