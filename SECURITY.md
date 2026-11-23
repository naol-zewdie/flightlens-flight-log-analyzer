# Security

## What FlightLens protects, and what it does not

FlightLens has **no login**. Anyone who can reach the web server can upload,
review, re-analyze and delete flights. Run it on `localhost` (the default) or
on a network where everyone is trusted. To share it more widely, put it behind
a reverse proxy that adds authentication and HTTPS.

Within that model the server defends itself:

- **Host check:** it answers only to the host names it was started for
  (`localhost`, plus `--allow-host` / `FLIGHTLENS_ALLOWED_HOSTS`). This blocks
  DNS-rebinding attacks from web pages open in your browser.
- **No cross-site changes:** a request that changes data and comes from another
  site (its `Origin` does not match) is refused.
- **Read-only mode:** `flightlens serve --read-only` refuses every change, for
  a public demo.
- **Isolated parsing:** each upload is parsed in its own process with a time
  limit, so a malicious or corrupt log cannot take the server down. Both
  parsers are fuzzed in CI.
- **Limits:** uploads must declare their size and are capped at 1 GB. Uploads
  are refused when the analysis queue is full or the disk is nearly full.
- **Files:** original logs are stored read-only under a name the server
  chooses. Backups are checked against their SHA-256 and refuse paths that
  would land outside the restore folder.
- **Headers:** every response carries `X-Content-Type-Options`,
  `Referrer-Policy` and frame protection. The HTML report and the web UI carry
  a Content-Security-Policy.

## Reporting a vulnerability

Please report security problems privately, through
[GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository ("Security" tab, "Report a vulnerability"), not in a public
issue. Include the FlightLens version, how to reproduce the problem and, if it
involves a log file, a minimal one that triggers it.

You will get an answer within a week. Fixes are released as a patch version
and credited in the changelog unless you prefer otherwise.

## Supported versions

Only the latest release gets security fixes.
