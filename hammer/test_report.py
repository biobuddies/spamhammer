from asyncio import get_running_loop
from collections import Counter
from email import message_from_bytes, policy
from logging import INFO
from os import getpid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from pytest import LogCaptureFixture, MonkeyPatch, mark, raises

from hammer import hammer
from hammer.hammer import Json
from hammer.management.commands import report as report_command
from hammer.management.commands import sweep
from hammer.management.commands.report import (
    compose_report,
    send_salesforce_report,
    signed_by_salesforce,
)

SPAM = (Path(__file__).parent / 'spam' / 'thealphapaths.eml').read_bytes()
AUTHENTICATED_SPAM = (
    b'Authentication-Results: biobuddi.es (amavis);\r\n'
    b' dkim=pass (2048-bit key) header.d=s51.y.mc.salesforce.com;\r\n'
    b' dkim=pass header.d=exacttarget.com\r\n' + SPAM
)


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
    cleanup.assert_awaited_once_with(
        'contact@news.unchartedvalues.com', dry_run=False, folder='Junk'
    )
    assert loops[0] is loops[1]


def test_report_sweeps_after_smtp_failure(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        report_command, 'report_after_delay', AsyncMock(return_value=(b'email', {}))
    )
    monkeypatch.setattr(
        report_command, 'send_salesforce_report', send := Mock(side_effect=OSError('SMTP failed'))
    )
    monkeypatch.setattr(sweep, 'sweep', cleanup := AsyncMock(return_value=Counter()))
    with raises(OSError, match='SMTP failed'):
        call_command('report', 'contact@news.unchartedvalues.com')
    send.assert_called_once_with(b'email')
    cleanup.assert_awaited_once_with(
        'contact@news.unchartedvalues.com', dry_run=False, folder='Junk'
    )


@mark.parametrize(
    ('header', 'signed'),
    (
        (
            b'Authentication-Results: biobuddi.es; dkim=pass header.d=alv174.mta.exacttarget.com',
            True,
        ),
        (
            b'Authentication-Results: imap.biobuddi.es; DKIM=PASS header.d=s51.y.mc.salesforce.com',
            True,
        ),
        (b'Authentication-Results: biobuddi.es; dkim=fail header.d=exacttarget.com', False),
        (b'Authentication-Results: sender.example; dkim=pass header.d=exacttarget.com', False),
        (b'Authentication-Results: biobuddi.es; dkim=pass header.d=exacttarget.com.example', False),
        (b'Received: from alv174.mta.exacttarget.com', False),
        (b'DKIM-Signature: v=1; d=s51.y.mc.salesforce.com;', False),
        (
            (
                b'Authentication-Results: biobuddi.es; dkim=none\r\n'
                b'Authentication-Results: biobuddi.es; dkim=pass header.d=exacttarget.com'
            ),
            False,
        ),
    ),
)
def test_signed_by_salesforce(header: bytes, signed: bool) -> None:
    assert signed_by_salesforce(header + b'\r\n\r\nAct now', 'imap.biobuddi.es') is signed


def test_compose_report() -> None:
    serialized = compose_report('busybuddy@biobuddi.es', SPAM).as_bytes()
    report = message_from_bytes(serialized, policy=policy.default)
    assert (report['From'], report['To'], report['Subject']) == (
        'busybuddy@biobuddi.es',
        'abuse@salesforce.com',
        'Abuse report: 83 parcels. One company.',
    )
    assert report.get_content_type() == 'multipart/report'
    assert report.get_param('report-type') == 'feedback-report'
    assert [part.get_content_type() for part in report.iter_parts()] == [
        'text/plain',
        'message/feedback-report',
        'message/rfc822',
    ]
    assert b'Feedback-Type: abuse\nUser-Agent: spamhammer\nVersion: 1\n' in serialized
    assert b'Received: from alv174.mta.exacttarget.com' in serialized


@mark.parametrize(
    ('submission', 'login_pair'),
    (
        ('submit://smtp.biobuddi.es', ('busybuddy@biobuddi.es', 'secret')),
        ('submit://buddy%40biobuddi.es:other@smtp.biobuddi.es', ('buddy@biobuddi.es', 'other')),
    ),
)
def test_send_salesforce_report(
    login_pair: tuple[str, str], monkeypatch: MonkeyPatch, submission: str
) -> None:
    monkeypatch.setattr(report_command, 'SMTP', smtp_class := MagicMock())
    monkeypatch.setenv(
        'SPAMHAMMER_EMAIL_URLS',
        f'imaps://busybuddy%40biobuddi.es:secret@imap.biobuddi.es, {submission}',
    )
    send_salesforce_report(AUTHENTICATED_SPAM)
    smtp_class.assert_called_once_with('smtp.biobuddi.es', 587, timeout=30)
    smtp = smtp_class.return_value.__enter__.return_value
    smtp.login.assert_called_once_with(*login_pair)
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert (message['From'], message['To']) == ('busybuddy@biobuddi.es', 'abuse@salesforce.com')


def test_salesforce_report_needs_submission(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://busybuddy:secret@imap.biobuddi.es')
    with caplog.at_level(INFO):
        send_salesforce_report(AUTHENTICATED_SPAM)
    assert 'No submit:// URL; skipped Salesforce Marketing Cloud' in caplog.text


def test_salesforce_report_needs_verified_signature(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv('SPAMHAMMER_EMAIL_URLS', 'imaps://busybuddy:secret@imap.biobuddi.es')
    with caplog.at_level(INFO):
        send_salesforce_report(b'Received: from mail.biobuddi.es\r\n\r\nHi')
    assert 'No verified Salesforce Marketing Cloud DKIM signature' in caplog.text
