# AGENTS.md — golden-beet-config (operational brief)

Always check the official documentation. Every feature must be tested. Keep everything logical and
well-coded (good practices).

Full docs: **README.md**. These are the rules so past incidents aren't repeated.

## What

A single Python app (**`gbc`**) driving **beets** (engine, via subprocess): recover a chaotic music
library into a clean **album** library (served by any Subsonic/DLNA player). Album-mode + AcoustID; only
complete, strong albums are kept — loose singletons stay parked in the source for manual curation.

## Architecture (one core, several doors)

`gbc run` (manual) and `gbc inbox` (cron, on drop) call the **same** pipeline
(`gbc/passes/pipeline.py`: **import → verify → qa**). beets does art/genres/replaygain/scrub/ftintitle
**natively during `beet import`** (`auto: yes` in `config.yaml`); gbc only adds **dedup** (before
import) + **sidecars** (after) + **verify** (AcoustID imposter -> quarantine) + **qa/anomaly** (audit). Passes in `gbc/passes/`; beets driven
through `beets.run_beet` (captures stdout **and stderr** — beet logs its `--pretend` plan to stderr);
config in `config.py`; single logger in `logs.py`; import lock (filelock) in `lock.py`; incremental
watermark (scopes qa) in `state.py`. `setup.sh` is the only bash (deps + `uv tool install --editable .` + `gbc init`).

- **Logs: one file** `$LOG_DIR/gbc.log`, append-only, every line tagged `[pass]` + run id — same for
  `run` and `cron` (never per-pass files). beets' own decisions stay in `import-decisions.log`.
- **Incremental:** qa scopes to items added since the last successful run (watermark);
  `--all` reprocesses everything. import is incremental via beets (`incremental: yes`).
- **Tooling:** `mise run test|lint|fix|audit`; tests are stdlib **unittest** (no pytest), no network.

## Config

Paths come from `config.env` (copy of `config.env.example`, gitignored) — use the vars, never hardcode:
`BEET`, `BEETSDIR`, `MUSIC_SRC`, `MUSIC_CLEAN`, `MUSIC_DUMP` (quarantine — never `rm`), `LOG_DIR`.
`config.py` sources `config.env` (resolves `$GBC_CONFIG` / `~/.config/gbc/` / repo root).

## CRITICAL RULES (learned the hard way)

1. **Never delete → move to `$MUSIC_DUMP`.** An accidental `rm` once destroyed thousands of tracks.
   If your filesystem supports snapshots (ZFS/btrfs/LVM), snapshot before any mass op; always `cp library.db`
   first. Only empty dir shells may be `rmdir`'d.
2. **Never bulk `modify`/`move`/`remove` without a query** (empty query hits the whole library).
   **Back up `library.db` first.**
3. **Confirm before any irreversible op**, in the same turn, just before running.
4. **Test on ~10 items before the whole library.**
5. **Scrub is mandatory on every move.** The `mime=None` crash (WMA/ASF) → strip the image FIRST via
   `helpers/` (`scan-scrub-crash.py`, then `mutagen-strip.py`/`strip-broken-art.py`), run with
   `uv run --with mediafile --with mutagen python helpers/<x>.py`. Never disable scrub.
6. **Never `--from-logfile`** (parent paths + names with `;` → recursion). Import the directory directly.
7. **`musicbrainz` must be in `plugins:`.** It is a separate metadata-source plugin; without it `chroma`
   yields no MusicBrainz candidates and fingerprint matching silently finds nothing.
8. **Never dedup on the db alone (`mb_trackid`).** The same recording on a studio album AND a
   compilation is legitimate. Disk-correlate (duration + bitrate), keep the best bitrate, never break an
   album folder.
9. **Compilations grouped by album TITLE, not albumartist**; then exclude generic titles +
   dominant-artist albums. "comp-heavy" (a hit appearing on many comps) is NOT a comp signal.

## Verified facts (do not re-debate)

- **Rate-limiting is NOT the cause of skips** (0 real 429/503 over 10k+ evaluations); skips = weak match +
  quiet mode refusing to guess. No personal AcoustID key needed (non-commercial use is free).
- **WMA** format is stored as "Windows Media" → query `format::Windows`, not `format:WMA`.

## Secrets

API keys in `beets/config.yaml` are redacted (`REPLACE_ME`); supply your own locally.

## Style

Concise, English. No emoji unless asked. `file:line` pointers, not pasted code that drifts. Confirm in
one word. Never `git commit` without explicit approval.
