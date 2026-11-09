from unittest.mock import MagicMock

from django.core.management import call_command
from pytest import CaptureFixture, MonkeyPatch

from hammer.management.commands import block


def test_show_sieve_prints_active_first(capsys: CaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://buddy:secret@imap.biobuddi.es')
    client = MagicMock()
    client.listscripts.return_value = ('USER', ['roundcube', 'old'])
    client.getscript.side_effect = lambda name: f'keep; # {name}\n'
    monkeypatch.setattr(block, 'Client', MagicMock(return_value=client))
    call_command('show_sieve')
    assert capsys.readouterr().out == (
        '# USER (active)\nkeep; # USER\n\n# old\nkeep; # old\n\n# roundcube\nkeep; # roundcube\n\n'
    )
    client.logout.assert_called_once()


def test_show_sieve_off(capsys: CaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    call_command('show_sieve')
    assert capsys.readouterr().out == ''
