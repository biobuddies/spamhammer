from asyncio import run
from datetime import UTC, datetime
from email import message_from_bytes, policy
from inspect import unwrap
from unittest.mock import AsyncMock

from aioimaplib import Response
from django.core.management import call_command
from pytest import CaptureFixture, MonkeyPatch, mark

from hammer.management.commands import block
from hammer.management.commands.notes import read_notes, record_note


def test_record_note_appends_dated_read_note() -> None:
    imap = AsyncMock()
    imap.append.return_value = Response('OK', [])
    run(unwrap(record_note)(imap, 'block', 'example.com', 'From contains example.com'))
    imap.create.assert_awaited_once_with('Spamhammer')
    message, folder, flags, date = imap.append.await_args.args
    note = message_from_bytes(message, policy=policy.default)
    assert (folder, flags, note['Subject'], note['X-Spamhammer-Target']) == (
        'Spamhammer',
        r'\Seen',
        'block example.com',
        'example.com',
    )
    assert note['Date'].datetime == date.replace(microsecond=0)
    assert abs(datetime.now(UTC) - date).total_seconds() < 60
    assert note.get_content() == 'From contains example.com\n'


@mark.parametrize(
    ('target', 'criteria'),
    (('', ('ALL',)), ('example', ('HEADER', 'X-Spamhammer-Target', '"example"'))),
)
def test_read_notes(criteria: tuple[str, ...], target: str) -> None:
    imap = AsyncMock()
    imap.select.return_value = Response('OK', [])
    imap.search.return_value = Response('OK', [b'1'])
    imap.fetch.return_value = Response(
        'OK',
        [
            bytearray(
                b'Date: Tue, 10 Nov 2026 01:30:00 +0000\r\nSubject: block example.com\r\n\r\n'
                b'From contains example.com\r\n'
            )
        ],
    )
    assert run(unwrap(read_notes)(imap, target)) == [
        '2026-11-10T01:30:00+00:00 block example.com: From contains example.com'
    ]
    imap.search.assert_awaited_once_with(*criteria)


def test_read_notes_without_folder() -> None:
    imap = AsyncMock()
    imap.select.return_value = Response('NO', [])
    assert run(unwrap(read_notes)(imap, '')) == []


@mark.parametrize('added', (True, False))
def test_block_records_only_added_rules(added: bool, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(block, 'folder_exists', AsyncMock(return_value=True))
    monkeypatch.setattr(block, 'update_script', AsyncMock(return_value=added))
    monkeypatch.setattr(block, 'record_note', record := AsyncMock())
    call_command('block', 'From', 'example.com')
    if added:
        record.assert_awaited_once_with(
            'block', 'example.com', 'From contains example.com: fileinto Spam'
        )
    else:
        record.assert_not_awaited()


def test_notes_off(capsys: CaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    call_command('notes')
    assert capsys.readouterr().out == ''
