# HA Timescale Bridge — phase 1

A Home Assistant custom integration for the documented TimescaleDB schema exposing a TimescaleDB **physical replica**
through authenticated HA WebSocket commands. No entities, MQTT dependency, Recorder replacement,
database writes, primary fallback, or database access from cards.

Version **0.2.0** adds UI setup and Electricity meter configuration. Existing YAML installations can be adopted through the UI. Replica acceptance checks remain pending;
read the [API](docs/api.md) and [verification notes](docs/verification.md) before use.

## Install from Marketplace / HACS

Requires Home Assistant Core **2026.11.2 or newer**; that is the tested Marketplace target.
The repository must be public for Marketplace/HACS installation, redownloads and updates.
A private GitHub connection is not a substitute for public repository access. Making it private
after installation leaves the installed files in place but prevents future downloads/updates.

1. Open **Settings → Marketplace → Add from link** and paste
   `https://github.com/JuzzWuzz/ha-timescale-bridge`. Choose **Integration** if prompted and
   install/download **Timescale Bridge**. A GitHub connection may be required for custom repositories.
   In older HACS, use **Custom repositories**, the same URL, and category **Integration**.
2. Use the published release when available; otherwise select the default branch **main**.
   The integration files belong in `custom_components/timescale_bridge/`.
3. Restart HA after installing the component. Open **Settings → Devices & services → Add integration → Timescale Bridge**.
4. Enter the Read Replica hostname, port, database, read-only role and password. Keep **verify-full** unless your installation explicitly requires unencrypted transport. HA verifies the replica before saving.

[Open in Marketplace](https://my.home-assistant.io/redirect/marketplace_repository/?owner=JuzzWuzz&repository=ha-timescale-bridge&category=integration)

The integration is not submitted to the public catalog. Marketplace/HACS installs the component
folder from the selected GitHub ref; no release ZIP asset is required for this layout.

## Pre-install compatibility

This version pins `asyncpg==0.31.0` and passes `datetime.timedelta` values to
PostgreSQL interval parameters for rolling queries. Keep `manifest.json` with the integration code
when copying it. Version 0.30.0 lacks CPython 3.14 musllinux wheels; 0.31.0 provides ARM64 and
x86_64 wheels. Binary-only installation and import passed in the stock HA 2026.11.2 x86_64 container
under its bundled constraints. ARM64 wheel selection passed, but ARM64 runtime was not exercised.
See [observed results and limits](docs/verification.md#pre-install-interval-and-driver-correction).

## Configuration

### Electricity meters

Open **Settings → Devices & services → Timescale Bridge → Configure**. Choose **Add meter**, select a catalog series whose metric is `energy`, and give it a display name. Choose **Independent grid feed** for each separately metered incoming connection, including channels on the same physical monitor. For a downstream meter, choose its immediate upstream meter. Add parents first. Use **Edit meter** to rename or move a branch. Move or remove children before removing their parent.

HA saves the hierarchy in the integration entry. Editing it changes how consumers group series. It does not alter historical measurements. Use **Reconfigure** to update replica settings while keeping the hierarchy. Series IDs belong to one catalog. Recheck every meter assignment if you switch to a different database or rebuild the catalog. Back up the HA configuration securely, including config entry storage, which contains the connection credentials.

### Existing YAML installations

The existing YAML setup and card commands continue to work. To enable the meter editor, leave the working YAML in place, choose **Add integration → Timescale Bridge**, then confirm **Adopt existing YAML connection**. HA copies the resolved settings into a single config entry and reuses the active connection. Once the entry is loaded, remove only the bridge YAML block and restart. When both exist, the config entry takes precedence, including for credential changes. A legacy configuration looks like this:

```yaml
timescale_bridge:
  host: replica.example.invalid
  port: 5432
  database: metrics
  username: !secret timescale_bridge_username
  password: !secret timescale_bridge_password
  sslmode: verify-full
```

In HA's local `secrets.yaml`, set `timescale_bridge_username` to the read-only role name and
`timescale_bridge_password` to its password. Never commit that file or put these values in a card.
Set `host` to the Read Replica's reachable hostname; the example above is intentionally not usable.

Use the dedicated `ha_timescale_bridge` role created below, with SELECT access to the catalog,
raw metrics and four continuous aggregates. Required Toolkit function access still needs verification
with the bridge queries. This integration does not create users, modify grants, refresh aggregates,
or execute DDL. The deployment operator must verify least privilege.

TLS defaults to `sslmode: verify-full`. The bridge passes that mode string to asyncpg 0.31.0,
which loads the CA file named by `PGSSLROOTCERT`, or, when unset, `~/.postgresql/root.crt` for
the HA process user. This path does **not** automatically use the environment's system trust store.
The CA file must be valid and readable **inside the HA container**; a file only on the Docker host
is insufficient. Mount the trusted CA file read-only and set `PGSSLROOTCERT` in the HA container's
process environment to its container path, or provide it at the default client path. This is not a
bridge YAML option. Missing/unreadable CA files cause verification setup to fail. The configured
`host` must also match the server certificate's identity. See the
[asyncpg 0.31.0 implementation](https://github.com/MagicStack/asyncpg/blob/v0.31.0/asyncpg/connect_utils.py).

`sslmode: disable` is an explicit option for a trusted LAN installation without TLS; it sends
credentials/data without transport encryption. No automatic TLS downgrade is performed.

For read-only TLS checks in an existing psql session:

```sql
SHOW ssl;
SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid();
```

`SHOW ssl` reports whether the server enables TLS support. The second query reports whether
**that psql session** uses TLS. Neither verifies the bridge's connection, its CA configuration,
or hostname verification; even a TLS-enabled server can accept an unencrypted session.

Administrators enter credentials in HA setup. The bridge read APIs expose no connection settings. All active authenticated HA users can read the complete bridge
catalog/data; HA entity-specific access rules do not filter these external series. Do not enable
this integration in a shared HA instance unless that access policy is appropriate.

Startup verifies `pg_is_in_recovery()` and fails closed for a primary. Each read repeats this check
inside a read-only transaction, including after reconnects. HA retries unavailable UI entries. For YAML-only setup, restart HA after correcting startup failures. Unloading the entry or stopping HA closes the pool.

## Read-only replica setup

Run the administrative setup on **Primary** using your existing database administrator role.
The integration does not run these commands. Replace `YOUR_ADMIN_ROLE` below:

```sh
docker exec -it timescaledb psql -U YOUR_ADMIN_ROLE
```

Switch databases inside psql:

```text
\c metrics
```

psql metacommands such as `\c` and `\password` need no semicolon. SQL statements below
use semicolons as usual.

Create the login role and grant access to the specific database objects:

```sql
BEGIN;
CREATE ROLE ha_timescale_bridge LOGIN;
GRANT CONNECT ON DATABASE metrics TO ha_timescale_bridge;
GRANT USAGE ON SCHEMA public TO ha_timescale_bridge;
GRANT SELECT ON TABLE
  public.devices_metrics,
  public.raw_metrics,
  public.cagg_metrics_thirty_mins,
  public.cagg_metrics_hourly,
  public.cagg_metrics_daily,
  public.cagg_metrics_monthly
TO ha_timescale_bridge;
COMMIT;
```

If grouping the SQL in a transaction as shown, commit it before setting the password. Use psql's
interactive password prompt; no password belongs in this README or a SQL command:

```text
\password ha_timescale_bridge
```

The SQL role, password change and grants are carried from the Primary to **Read Replica** by
physical replication; do not recreate them on the read-only replica. Container/network settings,
TLS and host authentication configuration are separate from these SQL grants. The bridge's single
connection target remains the replica, with **no primary fallback**.

### User-run smoke tests on the Read Replica

Connect as the new role using the interactive password prompt:

```sh
docker exec -it timescaledb psql -h timescaledb -p 5432 -U ha_timescale_bridge -d metrics -W
```

Inside psql, check identity and replica state, read the two energy catalog entries with blank
subtype, and test access to the 30-minute rollup:

```sql
SELECT current_user,
       current_database(),
       pg_is_in_recovery(),
       current_setting('transaction_read_only');

SELECT id, device, metric, subtype
FROM public.devices_metrics
WHERE device IN ('meter_a', 'meter_b')
  AND metric = 'energy'
  AND subtype = ''
ORDER BY device;

SELECT 1 FROM public.cagg_metrics_thirty_mins LIMIT 1;
```

Expected checks: the configured read-only role and database, recovery `true`, transaction read-only
`on`, and successful access to the catalog and rollup. The meter names above are synthetic examples;
substitute real catalog selections locally. A separate TCP connectivity check from the HA container
only verifies reachability, not authentication or TLS. These commands do not establish bridge query
parity or aggregate completeness. See [verification notes](docs/verification.md) for remaining checks.

## Development

The original baseline uses Python 3.13 / HA 2025.4.4. The tested Marketplace target, HA Core
2026.11.2, also passes isolated tests on Python 3.14.2, including authenticated test WebSocket
requests and shutdown cleanup. Validate the exact HA version used for installation. See the
[release-matched results and commands](docs/verification.md#automated-checks)
for the separate target-version environment. To run the original baseline:

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-test.lock
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

`requirements-test.txt` lists direct dependencies; `requirements-test.lock` records the exact tested
environment. No production credentials or replica connection are needed to run the tests. Database operations
are mocked; HA dispatch tests use the installed Home Assistant implementation. Do not infer that
Timescale SQL has been executed from a passing fixture suite.

A dashboard adapter can consume the catalog and query API without MQTT or placeholder entities.
Database lookup and query logic stay in this integration. No dashboard adapter is included here.
