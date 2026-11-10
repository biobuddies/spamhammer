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

from hammer.management.commands import block
from hammer.management.commands.block import folder_exists, insert_rule

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


def test_block_off(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    with caplog.at_level(INFO):
        call_command('block', 'Received', 'mta.exacttarget.com')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text


def test_block_backtest_off(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    with caplog.at_level(INFO):
        call_command('block', 'Received', 'mta.exacttarget.com', '--backtest')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text


@mark.parametrize('operation', ('CHECKSCRIPT', 'PUTSCRIPT', 'SETACTIVE'))
def test_block_surfaces_failed_writes(
    monkeypatch: MonkeyPatch, operation: str, sieve_server: Callable[..., Callable[[], bytes]]
) -> None:
    responses = {'CHECKSCRIPT': b'OK\r\n', 'PUTSCRIPT': b'OK\r\n', 'SETACTIVE': b'OK\r\n'}
    responses[operation] = b'NO "Refused"\r\n'
    sent = sieve_server(b'OK\r\n', *responses.values(), b'OK\r\n')
    monkeypatch.setattr(block, 'folder_exists', AsyncMock(return_value=True))
    with raises(ConnectionError, match=f'{operation} NO Refused'):
        call_command('block', 'From', 'sender@example.com')
    script = (
        'require "fileinto";\n'
        'if header :contains "From" "sender@example.com" { fileinto "Spam"; stop; }\n'
    )
    commands = [
        b'LISTSCRIPTS\r\n',
        b'CHECKSCRIPT {%d+}\r\n%s\r\n' % (len(script), script.encode()),
        b'PUTSCRIPT "spamhammer" {%d+}\r\n%s\r\n' % (len(script), script.encode()),
        b'SETACTIVE "spamhammer"\r\n',
    ]
    assert sent() == b''.join(commands[: list(responses).index(operation) + 2]) + b'LOGOUT\r\n'


def test_insert_rule_parses_and_matches_real_spam() -> None:
    spam = message_from_bytes(
        (Path(__file__).parent / 'spam' / 'thealphapaths.eml').read_bytes(), policy=policy.default
    )
    roundcube = (
        '# roundcube\nrequire ["fileinto","imap4flags"];\n'
        '# rule:[Spam: Move]\nif anyof (header :contains "x-spam-level" "***")\n'
        '{\n\tfileinto "Spam";\n}\n'
    )
    assert Parser().parse(insert_rule(roundcube, 'From', 'thealphapaths.com', 'Spam').encode())
    assert 'thealphapaths.com' in spam['From']


@mark.parametrize(
    ('lines', 'exists'),
    (([b'(\\HasNoChildren) "/" Spam', b'Completed'], True), ([b'Completed'], False)),
)
def test_folder_exists(exists: bool, lines: list[bytes]) -> None:
    imap = AsyncMock()
    imap.list.return_value = Response('OK', lines)
    assert run(unwrap(folder_exists)(imap, 'Spam')) == exists
    imap.list.assert_awaited_once_with('""', '"Spam"')


def test_block_requires_folder(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(block, 'folder_exists', AsyncMock(return_value=False))
    monkeypatch.setattr(block, 'update_script', update := AsyncMock())
    with raises(CommandError, match='No Junk folder'):
        call_command('block', 'From', 'sender@example.com', '--folder=Junk')
    update.assert_not_called()


def test_block_leaves_crlf_script_with_rule_alone(
    monkeypatch: MonkeyPatch, sieve_server: Callable[..., Callable[[], bytes]]
) -> None:
    script = b'require "fileinto";\r\n' + RULE.replace('Junk', 'Spam').encode().replace(
        b'\n', b'\r\n'
    )
    sent = sieve_server(
        b'"USER" ACTIVE\r\nOK\r\n', b'{%d}\r\n%s\r\nOK\r\n' % (len(script), script), b'OK\r\n'
    )
    monkeypatch.setattr(block, 'folder_exists', AsyncMock(return_value=True))
    call_command('block', 'Received', 'mta.exacttarget.com')
    assert sent() == b'LISTSCRIPTS\r\nGETSCRIPT "USER"\r\nLOGOUT\r\n'
