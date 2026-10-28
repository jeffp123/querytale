# querytale

Local, single-user CLI that syncs NextDNS logs into DuckDB and reports what was used when.
PLAN.md is the design and milestone list; build one milestone at a time.

## Commands

- `uv sync` to install; `uv run querytale --help`
- `uv run pytest -q`; `uv run ruff check . && uv run ruff format .`
- Try things against a throwaway home: `QUERYTALE_HOME=/some/tmp/dir uv run querytale ...`
  (never point experiments at the real `~/Library/Application Support/querytale`).

## Layout

- `src/querytale/api.py`: GET-only NextDNS client. Never add a non-GET call; tests enforce it.
- `store.py`: DuckDB schema, the shared staging -> `events` merge, `domain_daily`.
- `sync.py`: incremental/resumable API sync and the lock file. `importer.py`: CSV import.
- `services.py` + `presets/*.yaml`: service definitions; user YAML in `<home>/services` wins by id.
- `derive.py`: `domain_daily` and `minute_service` refresh after sync/import; full rebuild
  when the services fingerprint changes (about 2.5 s for a year).
- `sessions.py`: Detector A (sessions, confidence, app-running, overlaps, silent stretches).
- `quiet.py`: quiet windows (weekly and Shabbat via astral), calibration only.
- `timewindow.py`: `--last/--date/--from/--to` parsing. `cli.py`: typer commands;
  `textreport.py`: terminal rendering.

## Conventions

- Timestamps are stored as naive UTC `TIMESTAMP`; convert to local only for display and for
  local-day bucketing (`timezone(tz, timezone('UTC', ts))` in SQL).
- Both sources go through `staging` and `MERGE_SQL`; never write to `events` any other way,
  so API and CSV rows stay identical (tested in `test_csv_and_api_normalize_identically`).
- `events` is lossless at the natural key (profile, ts, domain, device_id). The router path
  (`nextdns-cli`) logs A/HTTPS a few ms apart rather than at the identical timestamp, so
  `minute_service` folds same (device, domain) events less than 1 s apart (`derive.FOLD`).
- Blocked lookups feed background patterns only (heartbeats, "app running", attribution),
  never sessions or unusual episodes.
- Escape every data value (hostnames, device and service names, regexes, errors) with
  `rich.markup.escape` before printing; `[a-z0-9]` in a regex is otherwise eaten as markup.
- Everything written is private: the CLI sets umask 077; dirs 0700, files 0600.
- Tests use synthetic fixtures (`tests/fakes.py`) and the fake API; never real exports.
- Line length 100; match the existing comment density (module docstrings explain the why).
