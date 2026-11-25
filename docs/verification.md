# Verification and limits

## Required schema and behavior

This integration expects `public.devices_metrics(id, device, metric, subtype)`,
`public.raw_metrics(time, device_metric_id, value)`, and the continuous aggregates
`cagg_metrics_thirty_mins`, `cagg_metrics_hourly`, `cagg_metrics_daily`, `cagg_metrics_monthly`.
Each rollup must expose `bucket_start`, `device_metric_id`, `value`, and `count`.
Confirm actual definitions, Toolkit access and SELECT grants before installation.

Bucket alignment uses `Africa/Johannesburg` as a system-specific query contract, independent of
browser timezone. The default interval/source policy is described in [the API](api.md). Weekly
queries require an explicit hourly/daily choice. Unaligned rollup ranges are rejected.
Rollups return stored values; raw gauges use arithmetic mean, and raw energy uses Toolkit counter
deltas within each bucket. Fewer than two raw energy observations return null/partial.
Missing buckets are null, observed zeros stay zero, and row presence never establishes completeness.
No primary fallback, schema migrations or aggregate refreshes are performed.

## Synthetic fixtures

The fixtures contain two synthetic energy series, `meter_a` and `meter_b`, with exact civil-day
boundary readings and deliberately sparse samples. Legacy bucket delta sums are 2 and 0, while
exact-boundary differences are 10 and 3. These figures demonstrate boundary/gap effects; they are
not production measurements or a general energy-total algorithm. Counter resets, missing boundary
samples and interpolation policy require separate validation.

## Automated checks

The test suites exercise range validation, pagination, parameter binding, bounds, cancellation,
read-only transactions, replica rejection, safe errors, zero/missing/partial coverage and lifecycle
cleanup. Release-matched tests also exercise authenticated HA HTTP/WebSocket requests against a
mock database and the actual Marketplace manifest validators.

Test environments:

| Environment | Result |
|---|---|
| Python 3.14.2, HA Core 2026.11.2, pytest-homeassistant 2026.11.2, asyncpg 0.31.0 | 60 passed on the cleaned tree |
| Python 3.13.9, HA Core 2025.4.4, asyncpg 0.31.0 | 58 passed; release-plugin tests skipped |

Do not install `pytest-homeassistant-custom-component` alongside the official release-matched
plugin. The pytest configuration disables the independent respx plugin so HA can initialize its
httpx alias. Dependency locks record the separate environments. The YAML-only cleanup path is HA shutdown. UI entries also support unload and reload.

```sh
uv venv --python 3.14.2 /tmp/ha-bridge-tests
uv pip install --python /tmp/ha-bridge-tests/bin/python -r requirements-test-2026.11.2.lock
/tmp/ha-bridge-tests/bin/python -m pytest -q
```

## Pre-install interval and driver correction

The rolling query binds PostgreSQL `$4::interval` using `datetime.timedelta` for each preset,
following [asyncpg's native codec mapping](https://magicstack.github.io/asyncpg/current/usage.html#type-conversion).
Eight regression cases verify complete arguments for energy and gauge queries.
The manifest and both test dependency sets pin asyncpg 0.31.0.

CPython 3.14 musllinux ARM64 and x86_64 wheels for 0.31.0 were downloaded and checksum-verified
against PyPI metadata; 0.30.0 had no corresponding wheels. Offline ARM64 target installation passed,
but ARM64 execution was not tested.

An offline binary-only install/import of asyncpg 0.31.0 passed in the stock HA 2026.11.2 x86_64
container, using its bundled package constraints. Observed runtime: Alpine 3.24.3, Python 3.14.10.
The image bundled asyncpg 0.32.0; after replacement with the integration's pin, all 1,659 installed
packages passed `uv pip check`. No HA deployment or startup was performed by that dependency check.

## Disposable SQL verification

A local disposable PostgreSQL 17.12 / TimescaleDB 2.32.1 / Toolkit 1.28.0 database reproduced the
string-interval codec failure and passed all four timedelta round trips. All eight generated rolling
queries (four presets, energy and temperature) returned expected buckets, counts and values,
including negative gauge values. The database used only synthetic rows and was removed afterward.

The opt-in script creates a new local container, exposes a random loopback port, accepts no external
DSN, and removes its container/volumes in `finally`. It captures generated SQL for execution on this
disposable primary; it does not disable the integration's production replica guard.

```sh
# Pull the disposable test image first; never point this check at a production database.
docker pull timescale/timescaledb-ha:pg17
PYTHONPATH=. /tmp/ha-bridge-tests/bin/python scripts/verify_disposable_timescale.py
```

## Remaining acceptance checks

- Confirm the installed HA version and the replica's PostgreSQL/Timescale/Toolkit versions.
- Verify the single replica target, SELECT-only role, CA file and certificate hostname match.
- Verify actual rollup definitions, refresh policies, real-time settings, lag and raw retention.
- Compare bounded raw and rollup queries, performance and rounding against the intended behavior.
- Test counter resets and missing boundary observations before changing energy semantics.
- Verify equivalent civil-day selection in browsers with different timezones in the consuming UI.
- Test actual Marketplace installation once public repository access is available; local package
  validation is not an end-to-end Marketplace download test.

No production credentials or private configuration are included. Example database, role and series
names are placeholders or synthetic fixtures; substitute installation-specific values locally.

## Electricity topology in 0.2.0

The isolated Python 3.14.2 / Home Assistant Core 2026.11.2 suite passed 63 tests with a mock replica. Tests submitted setup and meter forms over HA's authenticated HTTP configuration endpoints and read the hierarchy over its test WebSocket. A read-only HA account was denied access to the options flow. The synthetic hierarchy covered two independent roots, four levels, a sibling load, and a load on the second feed. Tests exercised reparenting, renaming, deletion, rejection of cycles, missing parents, duplicate and non-energy series, entry reload, settings reconfiguration, and adoption of a running YAML configuration without opening another pool.

Browser rendering, live replica reads, and upgrade on an installed HA system remain unverified. The historical Electricity chart and Sankey are future consumers of this topology.
