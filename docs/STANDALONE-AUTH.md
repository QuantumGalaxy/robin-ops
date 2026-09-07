# Connect your standalone robin-ops application

This connects the Python application directly to Robinhood. It does not use a
ChatGPT or Codex token, send account data to an LLM, or require an AI host to run.
The strategy remains deterministic and live execution remains disabled.

## Install and sign in (macOS/Linux)

From the repository with Python 3.11+:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,oauth]"
optionsagent auth-login
optionsagent auth-status
optionsagent mcp-probe
```

Sign in only on Robinhood's browser page. Keep your Agentic account unfunded for
initial quote testing. `mcp-probe` lists tool schemas; it does not place orders.
The application blocks mutation tools even if the server lists them.

Robinhood's metadata advertised dynamic public-client registration, S256 PKCE,
authorization-code and refresh-token grants, and only the `internal` scope when
checked on 2026-09-07. This scope is broad: the consent may include account data,
watchlists and Agentic-account trading. Local paper-mode restrictions do not turn
that grant into a provider-enforced read-only grant.

We request client name `robin-ops`. During the registration test Robinhood returned
**Robinhood Trading** as its display name with the requested local callback and
public-client authentication. The application preserves that returned name. If the
consent names ChatGPT, Codex or an unrelated client, stop and investigate.
Registration and login remain subject to Robinhood eligibility and approval.

## Credential handling

- Credentials are stored under `robin-ops.robinhood.oauth` in macOS Keychain or a
  supported native Linux Secret Service keyring. No plaintext fallback is allowed.
- The callback listens only on `127.0.0.1:8765`, validates a random state, checks an
  issuer when returned, and exchanges the code using an S256 PKCE verifier.
- Callback requests, tokens and provider error bodies are not logged.
- OAuth endpoints are restricted to the verified Robinhood URLs; redirects are refused.
- Expiring tokens refresh under an interprocess lock. Rotated refresh tokens are saved
  before returning the new access token. Failed refresh stops access rather than
  silently trading on an expired credential.
- `ROBINHOOD_MCP_TOKEN`, if explicitly set, overrides the keyring; the caller must
  manage that token's lifetime. Do not copy another application's OAuth credential.
- The current process-lock implementation requires POSIX; Windows is not supported.

```sh
optionsagent auth-logout
```

Logout removes local credentials. **It does not revoke the provider grant.** Revoke
access separately in Robinhood Security & Privacy / connected-agent settings.

## Next: live quotes with simulated orders

Create `config/paper-robinhood.yaml` using the normal strategy defaults and these
connection settings:

```yaml
mode: paper
data_provider: robinhood_mcp
broker:
  kind: paper
  starting_equity: 25000
state_dir: state/robinhood-paper
reference_data_file: state/reference.json
```

The reference file must contain real, fresh completed daily bars, historical ATM IV
rank, earnings and the exchange session. See [reference schema](REVIEW-FIXES.md).
An empty or fabricated file is not a substitute. Automatic reference-feed collection
and actual tool-payload validation remain integration work; login alone does not
make this a complete live-data paper MVP.

After those inputs are verified, run one supervised loop:

```sh
optionsagent run --config config/paper-robinhood.yaml --loops 1
optionsagent status --config config/paper-robinhood.yaml
```

Expected behavior: stale/unknown inputs prevent new entries; credentials never
appear in logs; paper holdings never appear as real Robinhood orders.

## Troubleshooting

- Keyring unavailable: install the `oauth` extra and unlock your native OS keyring.
- Port 8765 occupied: finish/close the other login before retrying.
- Browser login timeout: run `auth-login` again; it waits up to five minutes.
- HTTP 400/403 on registration: provider acceptance is required. Do not use a known
  ChatGPT/Codex client ID to bypass registration.
- TLS certificate errors: install the declared dependencies; certificate verification
  stays enabled through the `certifi` CA bundle.

Public metadata used:
- https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading
- https://agent.robinhood.com/.well-known/oauth-authorization-server

Tests cover PKCE, state/issuer mismatch, wrong client identity, endpoint restrictions,
invalid token lifetimes, token refresh/rotation and native-credential integration.
Real consent and authenticated quote access must be verified separately.

## Verification on 2026-09-07

Standalone browser authorization and native-keyring storage completed successfully.
The application enumerated 73 tools, retrieved an AAPL stock quote, and retrieved
one AAPL option chain containing 24 expiration dates. No balances were queried and
no orders were submitted. The stock quote carried a 2026-09-04 timestamp and must
not be described as a current-session price.

Actual tool schemas differ from the old adapter fixtures: equity/option responses
wrap quotes in `data.results[].quote`, option-chain lookup uses `underlying_symbol`,
and option-quote lookup uses `instrument_ids`. The existing market-data adapter
needs these verified schema updates before a full paper run. This authentication
change does not claim that adapter integration is complete.

175 automated tests and Ruff passed locally. Refresh logic is tested with fixtures;
expiry/refresh against the real provider has not yet been observed.
