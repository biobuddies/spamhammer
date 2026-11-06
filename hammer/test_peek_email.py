"""Exercise full-message lookup without a mail server."""

from asyncio import run
from inspect import unwrap
from io import StringIO
from unittest.mock import AsyncMock, patch

from aioimaplib import Response
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import mark, raises

from hammer.management.commands.peek_email import fetch_email


@mark.parametrize('subject', ('', 'Café offer', 'Café', 'offer', 'fé off'))
def test_fetch_email(subject: str) -> None:
    older = bytearray(b'From: sender@example.com\r\nSubject: Older\r\n\r\nOld body')
    latest = bytearray(
        'From: Sender <sender@example.com>\r\nSubject: Café offer\r\n\r\nBody'.encode()
    )
    other = bytearray(b'From: notsender@example.com\r\nSubject: Other\r\n\r\nOther body')
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.uid_search.return_value = Response('OK', [b'1 2 3'])
    imap.uid.return_value = Response('OK', [b'FETCH', older, latest, other, b'Done'])
    assert run(unwrap(fetch_email)(imap, 'sender@example.com', subject)) == latest
    imap.select.assert_awaited_once_with('INBOX')
    imap.uid.assert_awaited_once_with('FETCH', '1,2,3', 'BODY.PEEK[]')


@mark.parametrize('result', ('NO', 'OK'))
def test_fetch_email_missing_or_failed_search(result: str) -> None:
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.uid_search.return_value = Response(result, [b''])
    with raises(CommandError):
        run(unwrap(fetch_email)(imap, 'sender@example.com'))
    imap.uid.assert_not_awaited()


def test_peek_email() -> None:
    output = StringIO()
    raw = b'From: sender@example.com\r\nSubject: Offer\r\n\r\nComplete body\r\n'
    with patch(
        'hammer.management.commands.peek_email.fetch_email', new=AsyncMock(return_value=raw)
    ):
        call_command('peek_email', 'sender@example.com Offer', stdout=output)
    assert output.getvalue() == raw.decode()
