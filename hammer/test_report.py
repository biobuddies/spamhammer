from asyncio import get_running_loop
from collections import Counter
from logging import INFO
from os import getpid
from unittest.mock import AsyncMock, Mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer import hammer
from hammer.hammer import Json
from hammer.management.commands import report as report_command
from hammer.management.commands import sweep


@mark.parametrize('key', ('off', 'off until I find it'))
def test_report_off(caplog: LogCaptureFixture, key: str, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', key)
    with caplog.at_level(INFO):
        call_command('report', 'contact@news.unchartedvalues.com')
    assert 'SPAMHAMMER_EMAIL_URLS off' in caplog.text
    assert 'SPAMHAUS_API_KEY off' in caplog.text


@mark.parametrize(
    ('spam', 'logged'),
    (
        ('contact@news.unchartedvalues.com', "contact@news.unchartedvalues.com ''"),
        (
            'contact@news.unchartedvalues.com Solar is dead.',
            "contact@news.unchartedvalues.com 'Solar is dead.'",
        ),
        (
            'contact@news.unchartedvalues.com  Re: Solar is dead. ',
            "contact@news.unchartedvalues.com ' Re: Solar is dead. '",
        ),
    ),
)
def test_report_spam(caplog: LogCaptureFixture, logged: str, spam: str) -> None:
    with caplog.at_level(INFO):
        call_command('report', spam)
    assert f'Reporting {logged}' in caplog.text


def test_report_rejects_empty_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('SPAMHAMMER_EMAIL_URLS', raising=False)
    monkeypatch.setenv('SPAMHAUS_API_KEY', '')
    with raises(ImproperlyConfigured):
        call_command('report', 'contact@news.unchartedvalues.com')


def test_report_logs_cancel_command(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv('GITHUB_REPOSITORY', 'biobuddies/spamhammer')
    monkeypatch.setenv('GITHUB_RUN_ID', '42')
    with caplog.at_level(INFO):
        call_command('report', 'contact@news.unchartedvalues.com')
    assert 'Cancel: gh run cancel 42 --repo biobuddies/spamhammer' in caplog.text


def test_report_logs_kill_command(caplog: LogCaptureFixture, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv('GITHUB_RUN_ID', raising=False)
    with caplog.at_level(INFO):
        call_command('report', 'contact@news.unchartedvalues.com')
    assert f'Cancel: kill {getpid()}' in caplog.text


def test_report_rejects_negative_delay(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(hammer, 'fetch_spam', fetch := AsyncMock())
    with raises(CommandError, match='nonnegative'):
        call_command('report', 'contact@news.unchartedvalues.com', '--delay-seconds=-1')
    fetch.assert_not_awaited()


@mark.parametrize('submission', ({'id': 'abc'}, {}, None))
def test_report_sweeps_regardless_of_result(
    monkeypatch: MonkeyPatch, submission: Json | None
) -> None:
    loops = []

    async def submit(*_args: object) -> tuple[bytes, Json]:
        loops.append(get_running_loop())
        if submission is None:
            raise CommandError('Report failed')
        return b'email', submission

    async def move(*_args: object, **_kwargs: object) -> Counter[str]:
        loops.append(get_running_loop())
        return Counter()

    monkeypatch.setattr(report_command, 'report_after_delay', submit)
    monkeypatch.setattr(sweep, 'sweep', cleanup := AsyncMock(side_effect=move))
    if submission is None:
        with raises(CommandError, match='Report failed'):
            call_command('report', 'contact@news.unchartedvalues.com')
    else:
        call_command('report', 'contact@news.unchartedvalues.com')
    cleanup.assert_awaited_once_with('contact@news.unchartedvalues.com', dry_run=False)
    assert loops[0] is loops[1]


def test_report_sweeps_after_smtp_failure(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        report_command, 'report_after_delay', AsyncMock(return_value=(b'email', {}))
    )
    monkeypatch.setattr(
        report_command, 'email_reports', send := Mock(side_effect=OSError('SMTP failed'))
    )
    monkeypatch.setattr(sweep, 'sweep', cleanup := AsyncMock(return_value=Counter()))
    with raises(OSError, match='SMTP failed'):
        call_command('report', 'contact@news.unchartedvalues.com')
    send.assert_called_once_with(b'email')
    cleanup.assert_awaited_once_with('contact@news.unchartedvalues.com', dry_run=False)
