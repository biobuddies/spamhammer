"""Exercise list_email without a mail server."""

from asyncio import run
from inspect import unwrap
from logging import INFO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from aioimaplib import IMAP4_SSL, Response
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer.management.commands.list_email import ImapClient, list_email

UNSUBSCRIBE, ONE_CLICK = (
    line
    for line in (Path(__file__).parent / 'spam' / 'thealphapaths.eml').read_text().splitlines()
    if line.startswith('List-Unsubscribe')
)


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


def test_imap_session_raises_on_rejected_login(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://busybuddy:secret@imap.biobuddi.es')
    monkeypatch.setattr(IMAP4_SSL, 'create_client', Mock())
    monkeypatch.setattr(IMAP4_SSL, 'wait_hello_from_server', AsyncMock())
    monkeypatch.setattr(IMAP4_SSL, 'login', AsyncMock(return_value=Response('NO', [])))
    monkeypatch.setattr(IMAP4_SSL, 'select', select := AsyncMock())
    with raises(ConnectionError, match=r'imap\.biobuddi\.es LOGIN NO'):
        run(list_email())
    select.assert_not_awaited()


@mark.parametrize(
    ('method', 'arguments', 'message'),
    (
        ('select', ('INBOX',), 'INBOX SELECT NO'),
        ('search', ('UNSEEN',), 'SEARCH NO'),
        ('uid_search', ('UNSEEN',), 'UID SEARCH NO'),
        ('fetch', ('1', 'BODY.PEEK[]'), 'FETCH NO'),
        ('status', ('"Spam"', '(UIDVALIDITY)'), 'STATUS NO'),
        ('uid', ('MOVE', '1', 'Spam'), 'UID MOVE NO'),
    ),
)
def test_imap_client_checks_responses(
    monkeypatch: MonkeyPatch, method: str, arguments: tuple[str, ...], message: str
) -> None:
    monkeypatch.setattr(IMAP4_SSL, method, AsyncMock(return_value=Response('NO', [])))
    imap = object.__new__(ImapClient)
    with raises(CommandError, match=message):
        run(getattr(imap, method)(*arguments))


@mark.parametrize(
    ('headers', 'marker'),
    (
        ('', ''),
        (f'{UNSUBSCRIBE}\r\n', ''),
        (f'{UNSUBSCRIBE}\r\n{ONE_CLICK}\r\n', '\t[1C]'),
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
    imap.select.return_value = Response('OK', [])
    imap.search.return_value = Response('OK', [b'1 2'])
    imap.fetch.return_value = Response(
        'OK',
        [
            b'1 FETCH',
            bytearray(
                (
                    'From: "The Alpha Paths" <contact@news.thealphapaths.com>\r\n'
                    'Subject: 83 parcels. One company.\r\n' + headers
                ).encode()
            ),
            bytearray(
                b'From: contact@news.meadowbrookupdates.com\r\n'
                b'Subject: Fly Private for $169? The Secret Travel Writers Swear By\r\n'
            ),
            b'Done',
        ],
    )
    assert run(unwrap(list_email)(imap)) == [
        f'contact@news.thealphapaths.com 83 parcels. One company.{marker}',
        (
            'contact@news.meadowbrookupdates.com'
            ' Fly Private for $169? The Secret Travel Writers Swear By'
        ),
    ]
    imap.fetch.assert_awaited_once_with(
        '1,2', 'BODY.PEEK[HEADER.FIELDS (FROM SUBJECT LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)]'
    )
