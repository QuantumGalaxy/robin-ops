# AWS paper deployment

Ubuntu 24.04, Lightsail us-east-1, 1 GB RAM. The current hostname is
robin-ops.100-57-0-197.sslip.io and depends on retaining 100.57.0.197.
Attach a Lightsail static IP or configure a controlled domain before depending
on instance stop/start resilience. A new IP requires updating Caddy and
ROBIN_OPS_DASHBOARD_ORIGIN together. Reboots and application restarts are distinct
from stopping and starting a Lightsail instance.

The application stays paper-only. systemd starts the agent and loopback dashboard.
Caddy provides HTTPS and renews certificates. Public ports are 80 and 443;
8766 stays loopback-only. The random fragment key grants dashboard access and
control; keep the full URL private. Cookies are Secure, HttpOnly and SameSite=Strict.
Exact Host/Origin checks apply. The dashboard receives no OAuth encryption key.

OAuth records use Fernet authenticated encryption with atomic replacement on
refresh. A root-owned 0400 key at /etc/robin-ops/oauth-key is supplied through
systemd LoadCredential. Ciphertext lives in the service user's private
.config/robin-ops directory. No token is placed in command-line arguments,
environment variables, or a plaintext file. Root access can decrypt credentials;
encryption cannot protect against a compromised running host.

A fresh cloud OAuth sign-in avoids exporting the Mac refresh token. A private
SSH tunnel forwards localhost:8765 to the cloud callback; this port is never
publicly opened. The Mac runner stops only after cloud authentication is verified.
Only one runner may manage the migrated state.

Hourly backups use SQLite online backup and integrity checks and retain 168
archives in /var/backups/robin-ops. OAuth and dashboard secrets are excluded.
These backups are on the same instance and do not cover complete instance loss.
Copy archives off-host or enable separately billed Lightsail snapshots for that.
Restoring a checkpoint requires reconciliation before resuming.

Services: robin-ops-agent, robin-ops-dashboard, robin-ops-backup.timer, caddy.
Agent failures trigger delayed systemd restarts with a restart-rate limit.
Dashboard heartbeat checks expose stale-agent alerts. Remote push alerts are
not configured; inspect the dashboard to notice a halt.

## Daily Robinhood IV collection and source switching

The `robin-ops-iv-collector.timer` runs a separate oneshot service on boot and
one minute after each completed pass. It shares the encrypted cloud credential
backend, but never submits orders and cannot block the trading loop's exit checks.
It requires `robinhood_iv_daily_collection: true` in the paper profile.

The XNYS calendar determines holidays, daylight saving and early closes. Collection
begins 15 minutes before the scheduled close. For each configured stock it selects
a standard tradable ATM call, expiry 20–45 calendar days nearest 30 (earlier expiry
and lower strike break ties). The first accepted snapshot per session is immutable.
Both underlying and option timestamps must fall within that closing window and
within two minutes of each other; intraday option quotes must be at most two
minutes old. Positive finite IV, correct contract identity, standard contract,
uncrossed positive bid/ask, spread at most 20%, strike within 5% of spot, and IV at
most 1000% are required. These are data-quality limits, not entry-strategy limits.

Only missing stocks are retried, with least-recently attempted stocks first.
After-close recovery lasts 30 minutes and accepts only retained quotes timestamped
inside the original closing window. Restarted passes preserve both successes and
attempts. They cannot recover a historical closing quote that Robinhood no longer
serves. No gap is forward-filled. Failed requests are recorded without credentials;
missing-session and stale-collector warnings appear on the dashboard. There is no
external email/SMS alert delivery. A timer pass is killed after five minutes if hung;
the next scheduled pass retries. Review actual first-market-day acceptance and
resource use; tests cannot guarantee future vendor uptime.

`state/robinhood-paper/iv-robinhood-daily.sqlite3` stores observations with contract,
spot/option timestamps, receipt time, bid/ask and IV evidence. It is separate from
both DoltHub's `iv-research.sqlite3` and the legacy `iv.sqlite3`; neither legacy nor
imported vendor rows count toward the Robinhood threshold. The existing hourly
SQLite backups automatically include this new archive. Same-server backups do not
protect against loss of the server itself.

With `paper_iv_auto_switch: true`, each stock switches only when at least 200
validated completed-session observations exist in its trailing 252 XNYS sessions,
the latest completed session is present, all evidence matches the v1 definition,
and the IV range is not flat. The switch is persisted with its qualification
snapshot and logged in the audit. It never automatically falls back to DoltHub:
stale, insufficient or invalid Robinhood history subsequently blocks new entries
for that stock. Other stocks can remain on experimental DoltHub history. Hourly
DoltHub API updates exclude switched stocks; after all switch, no DoltHub download
is needed. Stored DoltHub data remains for review. Rank uses each source's own daily
series, never a mixture of current contract IV and a different vendor's history.

Install the service and timer from `deploy/`, reload systemd, and enable
`robin-ops-iv-collector.timer`. The dashboard shows collector health, collection
window, active source, Robinhood observation count and latest date. Turning off the
switch flag is an explicit configuration rollback, not an automatic fallback.
This feature remains restricted to paper trading and does not enable live orders.
