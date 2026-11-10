from collections.abc import Callable

from django.core.management import call_command
from pytest import CaptureFixture, MonkeyPatch


def test_show_sieve_prints_active_first(
    capsys: CaptureFixture, sieve_server: Callable[..., Callable[[], bytes]]
) -> None:
    sent = sieve_server(
        b'"roundcube"\r\n"USER" ACTIVE\r\n{3}\r\nold\r\nOK "Listscripts completed."\r\n',
        b'{14}\r\nkeep; # "USER"\r\nOK\r\n',
        b'{3}\r\nold\r\nOK\r\n',
        b'"keep; # roundcube"\r\nOK\r\n',
        b'OK "Logout completed."\r\n',
    )
    call_command('show_sieve')
    assert capsys.readouterr().out == (
        '# USER (active)\nkeep; # "USER"\n# old\nold\n# roundcube\nkeep; # roundcube\n'
    )
    assert sent() == (
        b'LISTSCRIPTS\r\nGETSCRIPT "USER"\r\nGETSCRIPT "old"\r\nGETSCRIPT "roundcube"\r\nLOGOUT\r\n'
    )


def test_show_sieve_off(capsys: CaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    call_command('show_sieve')
    assert capsys.readouterr().out == ''
