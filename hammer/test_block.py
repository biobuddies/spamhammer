"""Exercise Sieve rule insertion without a ManageSieve server."""

from logging import INFO
from unittest.mock import MagicMock

from django.core.management import call_command
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer.management.commands import block
from hammer.management.commands.block import insert_rule

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


@mark.parametrize('operation', ('putscript', 'setactive'))
def test_block_surfaces_failed_writes(monkeypatch: MonkeyPatch, operation: str) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://user:secret@imap.example.com')
    client = MagicMock()
    client.listscripts.return_value = (None, [])
    getattr(client, operation).return_value = False
    monkeypatch.setattr(block, 'Client', MagicMock(return_value=client))
    with raises(ConnectionError, match=operation.upper()):
        call_command('block', 'From', 'sender@example.com')
    client.logout.assert_called_once()
    if operation == 'putscript':
        client.setactive.assert_not_called()
