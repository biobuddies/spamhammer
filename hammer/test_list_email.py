"""Exercise list_email without a mail server."""

from asyncio import run
from inspect import unwrap
from logging import INFO
from unittest.mock import AsyncMock

from aioimaplib import Response
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer.management.commands.list_email import list_email


@mark.parametrize('urls', ('', ' , '))
def test_list_email_rejects_empty(monkeypatch: MonkeyPatch, urls: str) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', urls)
    with raises(ImproperlyConfigured):
        call_command('list_email')


@mark.parametrize('urls', ('imaps://a:b@c, imaps://d:e@f', 'imaps://a:b@c imaps://d:e@f'))
def test_list_email_requires_one_url(monkeypatch: MonkeyPatch, urls: str) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', urls)
    with raises(NotImplementedError):
        call_command('list_email')


def test_list_email_defaults_off(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    with caplog.at_level(INFO):
        call_command('list_email')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text


@mark.parametrize(
    ('headers', 'marker'),
    (
        ('', ''),
        ('List-Unsubscribe: <https://example.com/leave>\r\n', ''),
        (
            (
                'List-Unsubscribe: <https://example.com/leave>\r\n'
                'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n'
            ),
            '\t[1C]',
        ),
        (
            (
                'List-Unsubscribe: <mailto:leave@example.com>\r\n'
                'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n'
            ),
            '',
        ),
        (
            (
                'List-Unsubscribe: <https://[invalid/leave>\r\n'
                'List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n'
            ),
            '',
        ),
    ),
)
def test_list_email_marks_one_click(headers: str, marker: str) -> None:
    imap = AsyncMock()
    imap.search.return_value = Response('OK', [b'1 2'])
    imap.fetch.return_value = Response(
        'OK',
        [
            b'1 FETCH',
            bytearray(
                ('From: Sender <sender@example.com>\r\nSubject: Offer\r\n' + headers).encode()
            ),
            bytearray(b'From: other@example.com\r\nSubject: Other\r\n'),
            b'Done',
        ],
    )
    assert run(unwrap(list_email)(imap)) == [
        f'sender@example.com Offer{marker}',
        'other@example.com Other',
    ]
    imap.fetch.assert_awaited_once_with(
        '1,2', 'BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)]'
    )
