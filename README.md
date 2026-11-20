```mermaid
flowchart TD
    consent{Consented?}
    consent -->|Yes| unsubscribe[One-click unsubscribe]
    consent -->|No| report[Report to Spamhaus]
    unsubscribe --> sweep[Sweep matching INBOX messages to junk]
    report --> sieve[Write Sieve filter for future matching mail]
    sieve --> sweep
```

# Spamhammer

`junk_email` sweeps matching inbox messages to junk regardless of the unsubscribe or Spamhaus
result. The subject substring selects the message used to unsubscribe; it does not limit cleanup.
Dry runs inspect headers without moving mail. The report workflow places a Sieve `fileinto` filter
before the sweep. Installing that filter is still a separate `block` operation.

```bash
python -m manage junk_email MileagePlus_Partner@enews.united.com --dry-run --folder Spam
python -m manage junk_email MileagePlus_Partner@enews.united.com --folder Spam
```

Set `SPAMHAMMER_JUNK_FOLDER` for automatic cleanup; otherwise the destination is `Spam`. The folder
must already exist, and the server must support `UID MOVE`. If a move fails partway through, rerun
cleanup after fixing the cause. The move preserves message flags.

## Cleanup scope

Count dots in the domain after `@`:

* Two or more dots: clean up the exact subdomain, including every sender on it
* Fewer than two dots: clean up only the exact sender

IMAP substring search finds candidates; parsed From addresses must match the inferred scope before
any message changes. Deeper subdomains and similar-looking addresses are excluded. This rule counts
dots literally, including public suffixes such as `co.uk`.

```text
Moved 96 messages from enews.united.com to Spam: MileagePlus_Partner@, unitedairlines@, MyMileagePlus@, MileagePlus_Program@
Moved 10 messages from sender@one.domain to Spam
```

Dry runs use `Would move` instead of `Moved`. Domain output lists each affected sender once.

Mail-routing rules should record their scope, creation time, and last match. Review quiet rules
periodically; zero inbox messages alone does not prove a rule is unused. Retiring a rule can let
future mail reach inbox again.
