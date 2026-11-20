```mermaid
flowchart TD
    consent{Consented?}
    consent -->|Yes| unsubscribe["`**unsubscribe** via one-click endpoint`"]
    consent -->|No| report["`**report** to Spamhaus`"]
    unsubscribe --> sweep["`**sweep** matching INBOX messages to junk`"]
    report --> filter["`**filter** future matching mail with Sieve`"]
    filter --> sweep
```
