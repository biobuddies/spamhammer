"""Exercise Sieve rule insertion without a ManageSieve server."""

from asyncio import run
from collections.abc import Callable
from email import message_from_bytes, policy
from inspect import unwrap
from logging import INFO
from pathlib import Path
from unittest.mock import AsyncMock

from aioimaplib import Response
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises
from sievelib.parser import Parser

from hammer.management.commands import filter as filter_command
from hammer.management.commands.filter import check_folder, filter_matches, insert_rule

RULE = 'if header :contains "Received" "mta.exacttarget.com" { fileinto "Junk"; stop; }\n'


@mark.parametrize(
    ('script', 'updated'),
    (
        ('', f'require "fileinto";\n{RULE}'),
        (
            '# Roundcube\nrequire ["fileinto"];\nif true { keep; }\n',
            f'require "fileinto";\n# Roundcube\nrequire ["fileinto"];\n{RULE}if true {{ keep; }}\n',
        ),
        (f'require "fileinto";\n{RULE}', f'require "fileinto";\n{RULE}'),
    ),
)
def test_insert_rule(script: str, updated: str) -> None:
    assert insert_rule(script, 'Received', 'mta.exacttarget.com', 'Junk') == updated


def test_filter_off(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    with caplog.at_level(INFO):
        call_command('filter', 'Received', 'mta.exacttarget.com')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text


def test_filter_dry_run_off(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    with caplog.at_level(INFO):
        call_command('filter', 'Received', 'mta.exacttarget.com', '--dry-run')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text


@mark.parametrize('operation', ('CHECKSCRIPT', 'PUTSCRIPT', 'SETACTIVE'))
def test_filter_surfaces_failed_writes(
    monkeypatch: MonkeyPatch, operation: str, sieve_server: Callable[..., Callable[[], bytes]]
) -> None:
    responses = {'CHECKSCRIPT': b'OK\r\n', 'PUTSCRIPT': b'OK\r\n', 'SETACTIVE': b'OK\r\n'}
    responses[operation] = b'NO "Refused"\r\n'
    sent = sieve_server(b'OK\r\n', *responses.values(), b'OK\r\n')
    monkeypatch.setattr(filter_command, 'check_folder', AsyncMock())
    with raises(ConnectionError, match=f'{operation} NO Refused'):
        call_command('filter', 'From', 'unchartedvalues.com')
    script = (
        'require "fileinto";\n'
        'if header :contains "From" "unchartedvalues.com" { fileinto "Spam"; stop; }\n'
    )
    assert (
        sent()
        == b''.join(
            [
                b'LISTSCRIPTS\r\n',
                b'CHECKSCRIPT {%d+}\r\n%s\r\n' % (len(script), script.encode()),
                b'PUTSCRIPT "spamhammer" {%d+}\r\n%s\r\n' % (len(script), script.encode()),
                b'SETACTIVE "spamhammer"\r\n',
            ][: list(responses).index(operation) + 2]
        )
        + b'LOGOUT\r\n'
    )


@mark.parametrize(
    ('sample', 'header', 'value'),
    (
        ('meadowbrookupdates.eml', 'Received', 'from mta.news.meadowbrookupdates.com'),
        ('thealphapaths.eml', 'From', 'thealphapaths.com'),
    ),
)
def test_insert_rule_parses_and_matches_real_spam(sample: str, header: str, value: str) -> None:
    spam = message_from_bytes(
        (Path(__file__).parent / 'spam' / sample).read_bytes(), policy=policy.default
    )
    assert Parser().parse(
        insert_rule(
            (
                '# roundcube\nrequire ["fileinto","imap4flags"];\n'
                '# rule:[Spam: Move]\nif anyof (header :contains "x-spam-level" "***")\n'
                '{\n\tfileinto "Spam";\n}\n'
            ),
            header,
            value,
            'Spam',
        ).encode()
    )
    assert any(value in field for field in spam.get_all(header, []))


def test_check_folder_uses_exact_mailbox() -> None:
    imap = AsyncMock()
    run(unwrap(check_folder)(imap, 'Spam*'))
    imap.status.assert_awaited_once_with('"Spam*"', '(UIDVALIDITY)')


def test_filter_requires_folder(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        filter_command, 'check_folder', AsyncMock(side_effect=CommandError('STATUS NO'))
    )
    monkeypatch.setattr(filter_command, 'update_script', update := AsyncMock())
    with raises(CommandError, match='STATUS NO'):
        call_command('filter', 'From', 'unchartedvalues.com', '--folder=Junk')
    update.assert_not_called()


@mark.parametrize('dry_run', (True, False))
def test_filter_matching_mail(monkeypatch: MonkeyPatch, dry_run: bool) -> None:
    imap = AsyncMock()
    monkeypatch.setattr(filter_command, 'update_script', update := AsyncMock())

    async def search(*_criteria: str) -> Response:
        assert update.await_count == int(not dry_run)
        return Response('OK', [b'17 21'])

    imap.uid_search.side_effect = search
    assert (
        run(filter_matches(imap, 'Received', 'mta.exacttarget.com', 'Spam', dry_run=dry_run)) == 2
    )
    imap.status.assert_awaited_once_with('"Spam"', '(UIDVALIDITY)')
    imap.uid_search.assert_awaited_once_with('HEADER', '"Received"', '"mta.exacttarget.com"')
    if dry_run:
        imap.uid.assert_not_awaited()
    else:
        imap.uid.assert_awaited_once_with('MOVE', '17,21', '"Spam"')


def test_filter_dry_run_and_move_requires_move(monkeypatch: MonkeyPatch) -> None:
    imap = AsyncMock()
    imap.protocol.capabilities = set()
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://busybuddy:secret@imap.biobuddi.es')
    monkeypatch.setattr('hammer.hammer.ImapClient', lambda *_: imap)
    with raises(CommandError, match='Missing IMAP capabilities: MOVE'):
        call_command('filter', 'Received', 'mta.exacttarget.com', '--dry-run', '--move-existing')
    imap.status.assert_not_awaited()


def test_filter_leaves_crlf_script_with_rule_alone(
    monkeypatch: MonkeyPatch, sieve_server: Callable[..., Callable[[], bytes]]
) -> None:
    script = b'require "fileinto";\r\n' + RULE.replace('Junk', 'Spam').encode().replace(
        b'\n', b'\r\n'
    )
    sent = sieve_server(
        b'"USER" ACTIVE\r\nOK\r\n', b'{%d}\r\n%s\r\nOK\r\n' % (len(script), script), b'OK\r\n'
    )
    monkeypatch.setattr(filter_command, 'check_folder', AsyncMock())
    call_command('filter', 'Received', 'mta.exacttarget.com')
    assert sent() == b'LISTSCRIPTS\r\nGETSCRIPT "USER"\r\nLOGOUT\r\n'
