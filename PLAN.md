# NextDNS Activity Insights: Build Plan

Oct 28, 2026 · @Jeffrey Peck

## Summary

Build a local, single-user Python CLI that syncs NextDNS logs into a local DuckDB file and generates a self-contained interactive HTML report showing, per child (per NextDNS profile), when known services were actively used and which unusual sites appeared in clusters.

**Goals**

- Answer "what was this kid doing on devices over the last N hours or days" in under a minute, with one command.
- Separate real use of a service from background noise (update checks, telemetry, idle clients).
- Show overlaps, such as a game session running during a homework session.
- Surface rare, clustered domains that no preset knows about (for example, a half hour of hopping between browser-game sites).
- Be easy for other NextDNS users to install and run, with service definitions anyone can extend.

**Non-goals**

- No server, no accounts, no cloud, nothing listening on a network port by default.
- No alerts, schedules, rules or blocking. Review only.
- No claim of certainty. The report shows evidence; the parent interprets it.

## What the real data looks like

A one-year CSV export for one profile has 4,402,687 rows (765 MB, median about 12,400 rows a day), and it parses in about 10 seconds in plain Python, so volume is not a problem. The hard part is noise, and the export shows exactly which kinds.

CSV columns: `timestamp, domain, query_type, dnssec, protocol, client_ip, status, reasons, destination_country, root_domain, device_id, device_name, device_model, device_local_ip, matched_name, client_name`. The API's log objects carry the same fields plus a `tracker` name.

| Finding from the export | Consequence for the design |
| --- | --- |
| Most lookups arrive as A + AAAA + HTTPS triplets with the identical timestamp | Collapse rows with the same (timestamp, domain, device) into one "lookup event" before counting |
| The Steam client queries `p2p-*.discovery.steamserver.net` exactly once a minute whenever it is open, all night included. Steam domains appear in about 110,000 minutes of the year, and 93% of those minutes have exactly 1 lookup | "Steam domain seen" means the client is open, not that someone is playing. Detect steady heartbeats per service and measure activity above that baseline |
| The Xbox app hits `gamepass.com` at a steady 6 per 15 minutes | Same heartbeat handling; heartbeats are still useful as an "app running / computer awake" lane |
| 66 distinct (device id, name, model, local IP) combinations in one profile. One computer shows up under a hostname and several UUID names, and names are reused across device ids | Treat the profile as the person. Device is a secondary filter, never the main unit |
| `google.com` as a root hides `classroom.google.com` (7,400 events) and `docs.google.com` (6,700) | Service patterns match full hostnames, not just root domains |
| 345,000 rows are blocked; about 32,000 carry `category:gaming` | Blocked lookups are not distraction, but they still reveal background patterns: a blocked Steam client keeps checking in, backing off to a median of 12 minutes per hour instead of 60. Use them for heartbeats and the "app running" lane only, never for sessions or unusual episodes, and never present them as attempts |
| `client_name` is `apple-profile` for the phone and `nextdns-cli` for the router path; the phone produces 72% of rows | Show phone and computer lanes separately so phone chatter does not drown out computer use |
| A prototype rule (root seen on 3 or fewer of the prior 30 days, and 8+ lookups within 30 minutes) found 31 hits in 15 days: browser games, a game launch, homework platforms, and new-app analytics SDKs | The novelty idea works. It needs tracker demotion and clustering into episodes to be readable |
| Some sites live behind generic hosts (`*.cloudfront.net`, `*.supabase.co`, Akamai edges) | No log field links them to the site that caused them, but the hostname itself or learned co-occurrence attributes about 77% of these lookups (see generic-host attribution) |

## Architecture decision

Decision: a Python CLI plus a generated, self-contained interactive HTML report. The CLI does sync, analysis and precise text queries; the report is for looking. Both read the same local DuckDB file, so neither one limits the other.

&#91;embedded content: data pipeline · 2 sources, 1 store, 2 detectors, 2 outputs\]

Everything flows through one local table; both outputs read the same detector results, so the report and the terminal never disagree.

**Stack**

- Python 3.11+, installed with `pipx install` or `uv tool install`.
- DuckDB as the store: one file, fast over millions of rows, and has the window functions needed for minute buckets, gaps-and-islands sessions and rolling baselines.
- `httpx` for the NextDNS API, `typer` for commands, `rich` for terminal tables.
- Apache ECharts for charts, vendored and inlined into the report so it works offline with no CDN. It handles brushing and zooming on a time axis, Gantt-style custom series and heatmaps, and stays fast with 100,000+ points on canvas.
- `keyring` for storing the API key in the macOS Keychain.

**The interactive-vs-limiting tension, resolved:** the report is a static file that opens in any browser and needs nothing running. When the report is not enough, `query` gives exact answers in the terminal ("every lookup matching `crazygames` last Tuesday between 3 and 5 pm"), and can export CSV. A later, optional `serve` command (bound to 127.0.0.1 only) can add on-demand drill-down if the static report proves too limiting.

**Alternatives considered**

| Option | Why not (for v1) |
| --- | --- |
| Browser extension | Tied to one browser, awkward for a year of data, cookie-based auth is brittle, harder for others to install |
| Local web app with a background server | A daemon and an open port are exactly the security surface you want to avoid |
| Streamlit or Jupyter | Also a running server; notebooks are poor for non-technical users |
| Node/TypeScript | Workable, but the analysis side (DuckDB SQL, sessionization) is more natural in Python |
| Go single binary | Easiest install, thinner analysis tooling. Worth revisiting if packaging becomes the main complaint |

## Data ingestion

The NextDNS API is the only required source. Every run fetches just the minutes since the last sync, so "what happened in the last hour" costs one or two requests and a few seconds. A CSV import remains as an optional way to load a full year at once.

**API sync**

1. `init` calls `GET /profiles` to list profile ids and names, so nobody copies ids by hand.
2. Every `report`, `summary`, `unusual` and `query` starts with a sync (skip with `--no-sync`): `GET /profiles/:id/logs?raw=1&sort=asc&from=<high-water mark minus 2 min>&limit=1000`, following `meta.pagination.cursor` until it is null. Auth is the `X-Api-Key` header.
   - Use `raw=1`. The default API mode drops some query types, deduplicates and hides "noise", which would distort per-minute rates.
   - At about 12,000 rows a day, a day of catch-up is roughly 12 requests and the last hour is 1.
   - Back off on HTTP 429 (honor `Retry-After`, else exponential). A lock file prevents two syncs from running at once.
3. The first sync backfills 30 days, which for the sample profile is about 370,000 rows and 370 requests, with a progress bar. Pages arrive oldest first and the high-water mark advances after each page, so an interrupted backfill resumes where it stopped. `--backfill 90d` fetches more.
4. API rows include a `tracker` field that the CSV lacks. Store it; Detector B uses it to demote analytics noise.

**Why a local store is still needed.** The novelty baseline needs 30 days of history and the generic-host attribution learns from even more. Re-fetching that on every run would mean hundreds of requests and rate-limit trouble. So going API-first changes when data arrives, not the architecture: same CLI, same DuckDB file, same report. Until 30 days are synced, the report marks the unusual-sites section as "warming up".

**Optional CSV import.** `import FILE --profile alex` loads a full-year export and serves exactly the same purpose as the API backfill, only deeper. Imported rows land in the same `events` table and feed every baseline, heartbeat check and host attribution identically.

- Order does not matter: import before or after syncing. The natural key drops overlapping rows, and the sync high-water mark is simply the newest event from either source.
- When an import already covers the last 30 days, `init` skips the API backfill and syncs only what came after the export.
- CSV rows have no `tracker` value; tracker demotion falls back to the bundled SDK list for them.

**Normalization**

- Collapse rows sharing (profile, timestamp, domain, device id) into one lookup event; keep the set of query types seen.
- Store blocked lookups with their status; they feed background-pattern detection only (see the analysis engine).
- Store timestamps in UTC; render in the local time zone (from the system, overridable in config).
- Derive a `lane` per event: `phone` when the client is `apple-profile` or the model is a phone, `computer` otherwise, with config overrides by device id or name.

**Schema (DuckDB)**

| Table | Key columns | Purpose |
| --- | --- | --- |
| `profiles` | id, nextdns\_name, label | Profile id to child label |
| `events` | profile\_id, ts, domain, root, qtypes, status, reasons, tracker, device\_id, device\_name, device\_model, local\_ip, client, lane | One row per collapsed lookup; unique on (profile\_id, ts, domain, device\_id) |
| `sync_state` | profile\_id, high\_water\_ts, backfilled\_from, last\_run\_at | Incremental sync and backfill progress |
| `minute_service` | profile\_id, minute, lane, service, n\_allowed, n\_blocked | Per-minute counts per service, allowed and blocked kept apart; rebuilt for affected days after each sync |
| `domain_daily` | profile\_id, day, root, n, first\_seen\_ts, last\_seen\_ts | Feeds the novelty baseline |
| `host_attribution` | profile\_id, generic\_host, root, method, support, lift, updated\_at | Learned owner of each generic CDN host |

The database lives at `~/Library/Application Support/<app>/data.duckdb` on macOS (platformdirs elsewhere). Local data is kept until the user prunes it, independent of NextDNS retention.

## Analysis engine

Two detectors run over the same events: one finds sustained use of known services, the other finds rare domains that show up in clusters. Their outputs merge into a single timeline of sessions and episodes.

Blocked lookups never count as activity or as unusual sites: a blocked lookup reached nothing, so it is not a distraction. They do feed the background-pattern steps (heartbeats, the "app running" bar, host attribution), because a blocked client keeps checking in on a schedule. The report never frames them as attempts.

### Detector A: known-service sessions

1. **Match.** Each event is tagged with zero or more services using the hostname patterns in the service config (full hostname, not root).
2. **Strip heartbeats.** Each service may list heartbeat patterns (for Steam, `p2p-*.discovery.steamserver.net`). Events matching them do not count as activity; they feed a thin "app running" bar instead. Blocked heartbeats feed that bar too, but cadence is measured separately for allowed and blocked lookups: a blocked Steam client backs off from every minute to a median of 12 minutes per hour, and mixing the two would break the regularity test. An auto-detector also flags any hostname whose inter-arrival times are near constant (at least 20 occurrences, median gap of 30 s or more, interquartile range under 25% of the median) and suggests it as a heartbeat in `doctor` output.
3. **Score minutes.** Per profile, lane and service: activity = non-heartbeat lookups per minute, smoothed with a rolling 3-minute sum.
4. **Active minutes.** A minute is active when the smoothed count reaches the service's `min_rate`. Thresholds are per service because DNS caching hides steady use of single-host sites: Google Classroom mostly shows 1 to 2 lookups a minute, while YouTube in use shows dozens across `googlevideo.com` hosts.
5. **Sessions.** Gaps-and-islands: join active minutes separated by no more than `max_gap` (default 5 min); keep sessions of at least `min_duration` (default 5 min). Each session records start, end, lane, device ids, peak and mean rate, and its top hostnames.
6. **Confidence.** High: 15+ minutes and the mean rate is at least 2x `min_rate`. Medium: meets the thresholds. Low: a short or thin burst, shown faded and hidden by default.
7. **Overlaps.** Any time two sessions from different categories (for example `games` and `school`) intersect, emit an overlap record with both names and the shared minutes.

### Detector B: novelty bursts and episodes

1. **Baseline.** For each profile and root domain, count the days it was seen in the 30 days before the day being analyzed (from `domain_daily`).
2. **Rare.** A root is rare that day if it was seen on 3 or fewer of the prior 30 days. Roots already owned by a configured service are skipped (Detector A covers them).
3. **Burst.** A rare root bursts when it has 8 or more lookup events inside any 30-minute window. Record the window, count, hostnames and lane.
4. **Demote noise, do not drop it.** Bursts whose root is an ad or analytics tracker (the API's `tracker` field or a bundled list of SDK roots) go into a collapsed "new app activity" line, since a new analytics SDK usually means a new app was opened. Infrastructure roots (`akamai*`, `cloudfront.net`, `fastly.net`, `supabase.co` and similar) go through generic-host attribution (below) first.
5. **Episodes.** Bursts within 15 minutes of each other, on the same lane, merge into one episode. An episode titled "4 rare sites, 3:05 to 3:50 pm, mostly games" is how the browser-game-hopping case surfaces even when each site alone is small.
6. **Labels.** Each root gets a category from, in order: the user's own labels file, NextDNS block reasons such as `category:gaming`, and a bundled snapshot of the free UT1 domain-category database from Université Toulouse Capitole (relevant categories only, refreshed each release, so no extra network host; categories include `games`, `social_networks`, `audio-video` and more). Unlabeled roots show as "unknown" with the hostnames visible.

### Generic-host attribution

DNS logs contain nothing that links a lookup to the page or app that triggered it, but statistics recover the owner of about three quarters of generic-host lookups. Checked against the year export (227,000 lookups to 1,231 generic hosts):

- There is no referrer field. The CNAME field (`matched_name`) is filled only on blocked lookups.
- Exact-timestamp pairing fails: only 8.7% of generic-host lookups share a timestamp with a named lookup.
- Per-customer CDN hostnames are stable identifiers. Each CloudFront distribution gets its own `d<random>.cloudfront.net` name, so its owner can be learned from history.

| Method | How it works | Share of generic-host lookups |
| --- | --- | --- |
| Name in hostname | The owner's label sits inside the host: `nytimes.map.fastly.net`, `img-s-msn-com.akamaized.net`, `*.ls-apple.com.akadns.net` | 67% |
| Learned co-occurrence | Named roots seen on the same device from 10 s before to 2 s after each sighting. Requires 10+ sightings, the root present in 50%+ of them, and 20x more often than chance (lift). One CloudFront host sat next to its owner's main domain 96% of the time | 10% |
| Unattributed | Mostly Apple OS endpoints on shared Akamai edges; Apple traffic is everywhere, so lift stays low | 22% |
| Too rare to learn | Fewer than 10 sightings (557 hosts) | under 1% |

**Rules**

- Attributed hosts count toward the owner's service and are marked "inferred" in the report; `label` overrides any inference.
- The attribution table relearns after each sync from the full local history, which is one reason to keep more than 30 days.
- For hosts that stay unattributed, the drill-down lists the named lookups within 30 seconds on the same device, as context rather than a claim. That covers the "Instagram followed by a flurry of CloudFront" case: you see what surrounded it and judge.
- Rare one-off hosts, such as a browser game on its own `*.supabase.co` project, still appear in novelty bursts even though they cannot be named.

### Tuning

All thresholds live in the config with the defaults above. A `calibrate <service>` command prints the histogram of per-minute counts for a service over the last 30 days, which is how the Steam heartbeat (1 per minute, 93% of minutes) becomes obvious at a glance.

### Quiet windows (calibration only)

Config can name recurring windows when screens are expected to be idle, such as overnight or Shabbat. They are not rules or alerts; they are known-quiet ground truth that tunes the detectors.

- A built-in `shabbat` window runs from candle lighting to nightfall, computed offline from the latitude and longitude in config (the `astral` package). Fixed weekly windows cover everything else.
- Hostnames that recur inside quiet windows are background by definition and feed heartbeat detection.
- `calibrate` and `doctor` list any session found inside a quiet window, so each one is either a threshold to tighten or a real exception to look at.

A year of a weekly quiet window (Friday 8:30 pm to Saturday 5 pm, inside Shabbat year-round) shows why this works:

| Measure | Quiet window | Comparable weekday window |
| --- | --- | --- |
| Allowed lookups per hour | 122 | 272 |
| YouTube sessions (whole year) | 0 | 82 |
| Steam sessions | 0 | 24 |
| Xbox sessions | 0 | 47 |
| Supercell sessions | 1 | 45 |
| Social-app sessions (all on the phone) | 18 | 89 |

What remains in the quiet window is background: Apple and Windows services, WhatsApp, and the Steam and Xbox heartbeats from a computer left on. The social-app sessions carry the same host mix as weekdays (about 60% push and edge connections, 12% media), so DNS alone cannot separate background refresh from use for those apps. Phone social sessions are therefore capped at medium confidence.

## Report UI

The report is one HTML file per run (profile + time window) that opens straight to a swimlane timeline, with every chart sharing one zoomable time axis. Top to bottom:

1. **Header.** Child label, window ("last 48 h, Tue 2:00 pm to Thu 2:00 pm"), last sync time, and coverage gaps: stretches with zero lookups on a lane, which mean the device was off or asleep, or DNS bypassed NextDNS.
2. **Summary strip.** Active time per category (games, video, social, school, other), number of overlaps, number of unusual episodes.
3. **Timeline (main view).** One row per service, grouped by category, split into phone and computer lanes. Session bars are colored by category and faded by confidence. A thin hatched bar under a row shows "app running" from heartbeats, in a lighter shade where the lookups were blocked, so a schedule doing its job is visible at a glance. A separate "Unusual" row shows novelty episodes. Overlaps get a translucent vertical band across all rows. Brush or scroll to zoom.
4. **Frequency chart.** Stacked lookups per minute (or per 5 minutes on long windows) by service group plus "other", the NextDNS-style view you described, synced to the timeline's zoom. Toggles: hide heartbeats, log scale, lane filter.
5. **Heatmap.** Day by hour, one small grid per category, for windows of a week or more.
6. **Drill-down panel.** Clicking a session, episode or chart point opens: top hostnames with counts (inferred CDN owners marked), nearby named lookups for unattributed hosts, devices, first and last lookup, and the raw events in that window (embedded up to about 5,000 rows; past that it shows the equivalent `query` command).
7. **Unusual domains table.** Rare roots in the window with category, burst size, times and hostnames, plus a copy button for the `label` command so classifying a new site takes one paste in the terminal.

What gets embedded: aggregates for the whole window, plus raw events for sessions, episodes and unusual roots. A 7-day window is roughly 85,000 events and a few MB, which is fine. Above about 200,000 events the report embeds aggregates only.

## CLI commands and workflow

Everyday use is one command: `querytale report alex --last 24h`, which syncs, analyzes and opens the report. (`querytale` is free on PyPI as of today, and the repo will live at github.com/jeffp123/querytale.)

**First run**

1. `querytale init`: stores the API key in the macOS Keychain, lists profiles from the API, asks for a short label for each one to track (for example `alex`), and writes `config.toml`.
2. Still in `init`, an optional step: point it at a CSV export (or let it fetch one with `--download`) for a year of history. Skipping it is fine.
3. `init` then backfills through the API whatever the CSV did not cover (30 days without a CSV), with a progress bar.
4. `querytale report alex --last 7d` to sanity-check.

**Commands**

| Command | What it does |
| --- | --- |
| `init` | API key to Keychain, choose profiles, write config |
| `import FILE \| --download` | Optional: load a full-year CSV export for a deeper baseline |
| `sync [LABEL]` | Incremental API fetch for one or all profiles |
| `report LABEL --last 48h \| --from --to` | Sync, analyze, write and open the HTML report; `--no-sync`, `--no-open` |
| `summary LABEL --last 24h` | Same findings as plain text in the terminal: sessions, overlaps, unusual episodes |
| `query LABEL --last 7d --match REGEX` | Raw allowed events matching hostnames (blocked ones too with --include-blocked), as a table or `--csv` |
| `unusual LABEL --last 7d` | Novelty episodes only |
| `label DOMAIN CATEGORY`, `labels` | Classify a root domain once; future reports use it |
| `services` | List configured services and their patterns |
| `calibrate SERVICE LABEL` | Per-minute count histogram to tune thresholds |
| `doctor` | Checks config and sync health, suggests heartbeat hostnames and the biggest unclassified roots |

Time windows accept `--last 6h`, `--last 3d`, `--date 2026-10-27`, or `--from`/`--to`, all in local time.

## Service definitions

Services are small YAML files, one per service, so open-source contributors can add or fix one with a single pull request. Bundled presets ship in the package; files in the user's config folder override them by `id`.

```yaml
id: steam
name: Steam
category: games
domains:            # matches the domain and all subdomains
  - steampowered.com
  - steamcommunity.com
  - steamstatic.com
  - steamcontent.com
  - steamserver.net
heartbeat:          # counted as "app running", not as activity
  - regex: '^p2p-[a-z0-9]+\.discovery\.steamserver\.net$'
min_rate: 4         # lookups per rolling 3 minutes to count as active
min_duration: 5m
max_gap: 5m
verified: true      # thresholds checked against real logs
```

`domains` covers most cases; `hosts` (exact hostnames, needed for `classroom.google.com`) and `regex` entries cover the rest.

**Starter presets**

| Category | Services |
| --- | --- |
| games | Steam, Xbox / Game Pass, Epic, Battle.net, Riot, EA, Ubisoft, Roblox, Minecraft, Overwolf, Supercell, browser-game portals (CrazyGames, Poki, Coolmath Games, AddictingGames, Kongregate, Newgrounds, itch.io) |
| video | YouTube (`youtube.com`, `googlevideo.com`, `ytimg.com`), Twitch, Netflix, Disney+ |
| social | TikTok, Instagram, Facebook, Snapchat, Reddit, Discord |
| school | Google Classroom, Docs/Drive/Slides, Meet, Desmos, DeltaMath, Quizlet, Khan Academy, IXL, College Board, Canvas, Schoology |
| ai | ChatGPT, Claude, Gemini (kept neutral; could be homework or not) |
| indirect | Mobile game ad SDKs (AppLovin, Unity Ads, Vungle, ironSource): bursts suggest a mobile game in the foreground; always low confidence |

Presets start with `verified: false` and conservative thresholds. Only Steam, YouTube and Classroom get checked against your export in v1; the rest are tuned as real data comes in.

## Security and privacy

The tool makes outbound GET requests to `api.nextdns.io` and nothing else: no listener, no telemetry, no third-party requests from the report.

- **API key.** Stored in the macOS Keychain via `keyring`; `NEXTDNS_API_KEY` overrides it for scripting. Never written to config, logs or reports, and redacted from error messages.
- **Read-only by construction.** A NextDNS API key can change settings and clear logs. The HTTP client wrapper only permits GET and refuses any other method, with a test that enforces it.
- **No cookies or passwords.** The tool never asks for the NextDNS password or uses browser session cookies.
- **Report isolation.** All JavaScript, CSS and fonts are inlined. A Content-Security-Policy meta tag blocks every network fetch (`default-src 'none'` with inline script and style allowed), so an opened report cannot phone home.
- **Data at rest.** The database and reports are a child's browsing history. Create directories with mode 0700 and files with 0600, keep reports in the app's data folder by default, and offer `prune --older-than 400d`.
- **Repo hygiene.** `.gitignore` covers `*.duckdb`, `*.csv` and `reports/`. Test fixtures are synthetic, never real exports.

## Open source, README and limitations

The README has to set two expectations up front: the tool assumes one NextDNS profile per person, and DNS shows what was looked up, not what was looked at.

**Setup requirements to document**

- One profile per child, with every one of that child's devices configured to use it. A single shared home profile (for example, set only on the router) cannot separate people, and the device fields are too unstable to fix that.
- Logging enabled, with domain logging not dropped. NextDNS retention caps how much history an import can bring in.
- Where to find the API key (bottom of my.nextdns.io/account) and what it can do, which is why the tool stays read-only.

**Known limitations to state plainly**

- Caching: an OS or browser reuses a lookup for minutes, so steady use of a single-host site produces few lookups. Thresholds are per service for this reason.
- Prefetching: browsers resolve links on a page that was never clicked. One-off lookups mean little, which is why bursts need 8+ events.
- Long-lived connections: games often talk to servers by IP after login, so play can look quieter than a store visit. Launcher and overlay traffic (Steam, Overwolf, Xbox) carry the signal.
- Coverage gaps: VPNs, a browser's own encrypted DNS, iCloud Private Relay, or a device off the profile all produce silence. The report shows silence explicitly rather than implying "nothing happened".
- Shared devices and generic CDN hosts cannot be attributed.

**Project basics**

- MIT license. Contributions welcome mainly as service YAML files, each with a note on how it was calibrated.
- Tests run on synthetic fixtures that reproduce the patterns found here (1-per-minute heartbeats, query-type triplets, rare bursts), never on real logs.
- One sentence in the README on intent: built for parents of minors as a conversation starter, not proof.

## Milestones for Claude Code

Build in seven milestones, each ending in something you can run, and check each one against your real export before moving on. Save this plan as `PLAN.md` in the repo root and ask Claude Code to work one milestone at a time, keeping a `CLAUDE.md` with conventions as it goes.

1. **Scaffold.** `uv` project, `pyproject.toml`, `typer` entry point, `ruff`, `pytest`, `.gitignore`, config loading, Keychain storage, GET-only API client.
   - Done when: `querytale --help` works and a test proves the client refuses POST, PATCH and DELETE.
2. **API sync and store.** `init` (lists profiles), `sync` with `raw=1`, cursor paging, high-water mark, 30-day resumable backfill, 429 back-off, lock file; triplet collapse into DuckDB; `domain_daily`; the `query` command.
   - Done when: the backfill survives a Ctrl-C and resumes without duplicates, a last-hour sync takes under 5 seconds, and one sample hour matches the NextDNS dashboard closely.
3. **CSV import.** `import FILE` and `import --download`, normalized identically to API rows, merged by natural key, with `init` offering it and skipping the API backfill it covers.
   - Done when: importing the year export takes under 60 seconds, a sync afterward adds only newer rows, and re-importing adds nothing.
4. **Detector A and `summary`.** YAML loader, Steam, YouTube and Classroom presets, heartbeats with separate allowed and blocked cadence, sessions from allowed lookups only, confidence, overlaps, `calibrate`.
   - Done when: an overnight idle-Steam stretch yields zero game sessions, blocked lookups never create a session but do show as "app running", sessions match 3 days you can personally vouch for, and calibrate finds no video or game sessions inside the Shabbat quiet window.
5. **Detector B, attribution, `unusual` and `label`.** Rarity baseline, bursts, tracker demotion, episodes, category sources, generic-host attribution.
   - Done when: a synthetic fixture of 3 game portals over 40 minutes produces exactly one episode, tracker-only bursts land in the collapsed line, and attribution on real data lands near the 67 / 10 / 22% split measured here.
6. **HTML report.** Inlined ECharts, timeline, frequency chart, heatmap, drill-down, unusual table, CSP.
   - Done when: a 7-day report opens offline, DevTools shows zero network requests, the file is under 10 MB and it renders in under 2 seconds.
7. **Open-source polish.** README, the remaining presets, `doctor`, `prune`, CI on GitHub Actions, and a clean-machine `pipx install` test.

Milestone 7 is optional for your own use. CSV import is optional for users but comes early in the build, because a year of real data makes milestones 4 and 5 much easier to tune.

## Decisions

- **Name: `querytale`.** DNS queries plus "telltale": the queries tell the tale, and telltale signs are what the tool surfaces. Said aloud it is also "query tail", as in tailing a log of queries. It works for parents and for personal screen-time checks alike. Free on PyPI today.
- **Category list:** bundle a snapshot of the UT1 database (relevant categories only) so the tool never contacts any host but NextDNS.
- **One profile per report.** No side-by-side children.
- **Keep every report:** `reports/<label>/<date>_<time>_<window>.html`, never overwritten; `prune` removes old ones.
- **Ground truth:** the Shabbat quiet window, plus 3 days you can vouch for in milestone 4.

* [x] Repo: github.com/jeffp123/querytale, confirmed available. Other unrelated uses of `querytale` exist elsewhere; an overlapping name is fine for this project.

