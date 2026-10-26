"""Exercise hammer's graceful degradation without network access."""

from logging import INFO

from django.core.management import call_command
from pytest import LogCaptureFixture, MonkeyPatch, mark


@mark.parametrize('key', ('off', 'off until I find it'))
def test_hammer_off(caplog: LogCaptureFixture, key: str, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', key)
    with caplog.at_level(INFO):
        call_command('hammer')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text
    assert 'SPAMHAUS_API_KEY off' in caplog.text
