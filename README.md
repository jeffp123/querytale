# querytale

**See what your NextDNS queries tell.** querytale syncs a NextDNS profile's logs into a local
database and shows, per person, when known apps and sites were actually used, what ran in the
background, and which unusual sites appeared in clusters, as a terminal summary or a
self-contained interactive HTML report.

Built for parents of minors as a conversation starter, not proof.

```
querytale report alex --last 24h
```

## Two things to know first

1. **One NextDNS profile per person.** querytale treats a profile as one person. Every device
   that person uses should be set to their profile. A single shared home profile (for example,
   only on the router) can't separate people, and the device fields NextDNS records are too
   unstable to fix that.
2. **DNS shows what was looked up, not what was looked at.** The report is evidence for you to
   interpret. See [Limitations](#limitations).

## Install

Requires Python 3.11 or newer.

```
pipx install git+https://github.com/jeffp123/querytale
# or
uv tool install git+https://github.com/jeffp123/querytale
```

## Set up

1. In NextDNS, give each child their own profile and turn on logging (keep domain logging on).
   Log retention limits how much history you can import later.
2. Copy your API key from the bottom of <https://my.nextdns.io/account>.
3. Run `querytale init`. It stores the key in the macOS Keychain (or your OS keyring), lists
   your profiles, asks for a short label for each one you want to track (say `alex`), offers to
   import a CSV export for up to a year of history, and backfills 30 days through the API.

Then:

```
querytale report alex --last 7d     # interactive HTML report, opens in your browser
querytale summary alex --last 24h   # the same findings in the terminal
```

Every command syncs first (a few seconds; skip with `--no-sync`).

## Commands

| Command | What it does |
| --- | --- |
| `init` | Store the API key, choose profiles, run the first sync |
| `sync [LABEL] [--backfill 90d]` | Fetch new log entries for one or all profiles |
| `import FILE --profile LABEL` / `--download` | Load a full CSV export for a deeper baseline; safe to repeat |
| `report LABEL --last 48h` | Write and open the HTML report (`--no-open`, `--out FILE`) |
| `summary LABEL --last 24h` | Sessions, app-running time, overlaps, unusual sites, silent stretches |
| `unusual LABEL --last 7d` | Unusual-site episodes only |
| `query LABEL --last 7d --match REGEX` | Raw lookups matching a hostname pattern (`--csv`, `--include-blocked`, `--lane`) |
| `label DOMAIN CATEGORY` / `labels` | Classify a site once; every future report uses it (`ignore` hides it) |
| `services` | List service definitions and their patterns |
| `calibrate SERVICE LABEL` | Per-minute count histograms for tuning a service's thresholds |
| `doctor [LABEL]` | Config and sync health, heartbeat suggestions, busiest unclassified sites |
| `prune --older-than 400d` | Delete old lookups and reports from this computer |

Time windows: `--last 90m|6h|3d|2w`, `--date 2026-10-27`, or `--from '2026-10-27 15:00' --to ...`,
all in local time.

## How it works

- **Sync.** The NextDNS API is read with `raw=1` (no server-side de-duplication), oldest first,
  resuming from the newest stored lookup. Rows sharing a timestamp, hostname and device (the A,
  AAAA and HTTPS queries of one lookup) collapse into one event.
- **Known services.** Each service is a small YAML file of hostname patterns. Lookups per
  minute are smoothed over 3 minutes; a minute is active when it reaches the service's
  `min_rate`, and active minutes join into sessions. Heartbeats (an idle Steam client checks in
  once a minute, all night) are recognized and shown as "app running" instead of use. Blocked
  lookups never count as use.
- **Unusual sites.** A site seen on 3 or fewer of the prior 30 days that gets 8 or more lookups
  within 30 minutes forms a burst; bursts close together form an episode, such as "4 rare sites,
  mostly games". Ad and analytics SDK bursts are listed separately as "new app activity".
- **CDN hosts.** Lookups to generic hosts (`*.cloudfront.net`, Akamai edges) are attributed to
  their owner when the owner's name is in the hostname or when the host reliably appears right
  after the owner's own lookups. Attributions are marked "inferred".
- **Categories** come from your own labels, NextDNS block reasons, and a bundled snapshot of
  the UT1 category lists.

## Privacy and security

- The only network requests are GETs to `api.nextdns.io` (plus the export file when you use
  `import --download`). No telemetry, nothing listens on a port.
- A NextDNS API key can change settings and delete logs, so the HTTP client refuses every method
  except GET, and a test enforces it. The key lives in the OS keychain (`NEXTDNS_API_KEY`
  overrides it) and never appears in config, logs or reports.
- Data, config and reports are a child's browsing history: they stay in
  `~/Library/Application Support/querytale` (the platform equivalent elsewhere), readable only by
  you. Reports inline everything and carry a Content-Security-Policy that blocks all network
  access, so an opened report cannot phone home.

## Limitations

- **Caching.** Operating systems and browsers reuse lookups for minutes, so steady use of a
  single-host site produces few lookups. Thresholds are per service for this reason.
- **Prefetching.** Browsers resolve links that were never clicked. One-off lookups mean little,
  which is why unusual sites need clusters.
- **Long-lived connections.** Games often talk to servers by IP after login, so play can look
  quieter than a store visit. Launchers and overlays (Steam, Overwolf, Xbox) carry the signal.
- **Background refresh.** Social apps refresh with the same hosts as real use, so phone social
  sessions are capped at medium confidence.
- **Coverage gaps.** VPNs, a browser's own encrypted DNS, iCloud Private Relay, or a device off
  the profile all produce silence. The report shows silence explicitly instead of implying that
  nothing happened.
- **Shared devices** and many generic CDN hosts cannot be attributed to a person or a site.

## Adding or fixing a service

Services live in `src/querytale/presets/`, one YAML file each. Put your own in the `services`
folder inside the data folder to override a preset by `id`.

```yaml
id: steam
name: Steam
category: games        # games, video, school, social, ai, or anything else
domains:               # a domain and all its subdomains
  - steampowered.com
hosts:                 # exact hostnames
  - store.steampowered.com
heartbeat:             # background check-ins: "app running", not use
  - regex: '^p2p-[a-z0-9]+\.discovery\.steamserver\.net$'
min_rate: 4            # lookups per rolling 3 minutes to count as active
min_duration: 5m
max_gap: 5m
phone_max_confidence: medium   # optional caps
max_confidence: high
verified: false        # true once thresholds were checked against real logs
```

Pull requests with new or improved service files are the most useful contribution. Please say
how you calibrated it: `querytale calibrate <service> <label>` prints the per-minute histogram,
and `querytale doctor` suggests heartbeat hostnames.

## Development

```
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Tests use synthetic fixtures only, never real exports. `scripts/build_ut1_snapshot.py` refreshes
the bundled category snapshot before a release.

## License

MIT for querytale itself. Bundled third-party data and code keep their licenses:

- Category data derived from the [UT1 blacklists](https://dsi.ut-capitole.fr/blacklists/),
  Université Toulouse Capitole, CC BY-SA 4.0 (`src/querytale/data/NOTICE`).
- [Apache ECharts](https://echarts.apache.org/) 5.6.0, Apache License 2.0
  (`src/querytale/vendor/`).
