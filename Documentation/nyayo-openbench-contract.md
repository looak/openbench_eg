# OpenBench → nyayo contract

This document defines the contract between the OpenBench fork and nyayo for live match reporting.

## Scope

OpenBench produces state updates. nyayo stores and renders the live chart and match viewer. The OpenBench fork should never contain viewer logic or graph calculations.

## Transport

- Method: PUT
- Path: /api/matches/{id}
- Auth: Authorization: Bearer <NYAYO_TOKEN>
- Content-Type: application/json

## Timing

OpenBench sends a complete snapshot after each relevant state change. The report is queued and sent after the transaction commits so it does not block the worker.

Relevant events:
- a result batch updates a test
- a worker bench error finishes the test
- a workload is manually stopped, restarted, deleted, or restored

If nyayo is down, the producer retries later. The worker keeps running; the next report carries the full latest state.

## Match id

- Format: openbench-<instance>-<test.id>
- Example: openbench-bobby-123
- <instance> is the value of NYAYO_INSTANCE from the OpenBench deployment config.

## Schema version

- schema_version: integer
- current value: 1

## Payload

```json
{
  "schema_version": 1,
  "id": "openbench-bobby-123",
  "kind": "sprt",
  "state": "running",
  "updated_at": "2026-10-16T12:34:56Z",
  "producer": {
    "kind": "OpenBench",
    "url": "/test/123/",
    "instance": "bobby"
  },
  "subject": {
    "engine": "Dev Engine",
    "name": "Dev Engine",
    "repo": "https://example.com/dev.git",
    "sha": "abc123",
    "bench": 2000,
    "options": "Threads=1",
    "time_control": "40/20",
    "network": "dev-net",
    "book": "book"
  },
  "opponents": [
    {
      "engine": "Base Engine",
      "name": "Base Engine",
      "repo": "https://example.com/base.git",
      "sha": "def456",
      "bench": 1800,
      "options": "Threads=1",
      "time_control": "40/20",
      "network": "base-net"
    }
  ],
  "results": [
    {
      "games": 18,
      "wins": 9,
      "losses": 3,
      "draws": 6,
      "LL": 12,
      "LD": 8,
      "DD": 5,
      "DW": 4,
      "WW": 3,
      "crashes": 1,
      "timeloss": 2
    }
  ],
  "sprt": {
    "lower": -5.0,
    "upper": 5.0,
    "alpha": 0.05,
    "beta": 0.05,
    "current_llr": 0.4,
    "lower_llr": -1.0,
    "upper_llr": 1.0,
    "result": null
  },
  "settings": {
    "dev_time_control": "40/20",
    "base_time_control": "40/20",
    "book_name": "book",
    "adjudication": "movecount=3 score=400",
    "syzygy_wdl": "OPTIONAL",
    "syzygy_adj": "OPTIONAL",
    "scale_method": "BASE",
    "scale_nps": 0
  },
  "machines": [
    {
      "machine_id": 7,
      "user": "alice",
      "games": 18,
      "wins": 9,
      "losses": 3,
      "draws": 6,
      "crashes": 1,
      "timeloss": 2,
      "updated_at": "2026-10-16T12:34:56Z"
    }
  ],
  "error": false,
  "finished": false
}
```

## Field meaning

### kind

- `sprt` for SPRT tests
- `games` for fixed-game tests
- later: `spsa`, `datagen` if nyayo defines them

### state

- `running`: not finished
- `finished`: passed or failed
- `stopped`: finished but neither passed nor failed, or manually stopped/deleted
- `error`: finished without pass/fail and the newest worker-side event is newer than the newest admin-side event

### subject

The dev side of the test: engine, repo, commit, options, time control, network, and book.

### opponents

The base side of the test, as a single list entry in `opponents[0]`.

### results

The cumulative totals for the test at this snapshot:
- games
- wins
- losses
- draws
- LL, LD, DD, DW, WW
- crashes
- timeloss

### sprt

For SPRT tests only:
- lower, upper
- alpha, beta
- current_llr
- lower_llr, upper_llr
- result: "passed", "failed", or null while running

### settings

OpenBench test parameters that matter to the viewer and to rendering the test context:
- dev_time_control
- base_time_control
- book_name
- adjudication values
- syzygy values
- scale_method and scale_nps

### machines

Per-machine subtotal rows as reported by the result rows for the test. This is useful for debugging and per-machine breakdowns in nyayo.

## Contract boundaries

- OpenBench owns the producer logic only.
- nyayo owns the database schema, viewer, graph, and forecast calculations.
- Each update is a full snapshot, not a delta.
- The same id may be repeated; nyayo should replace the prior snapshot for that match id.

## Frontend expectations for nyayo

Nyayo should:
- store the latest document per match id
- treat the document stream as a time series
- plot cumulative games over time
- estimate throughput from recent game-rate data
- compute estimated games left and time left to threshold from the current pace
- show the SPRT estimate as a forecast, not a hard stop time

## Not in scope for round one

- SPSA tuning reporting
- DATAGEN reporting
- any graph logic in the OpenBench fork
