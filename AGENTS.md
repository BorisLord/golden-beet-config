# AGENTS.md — golden-beets-config (operational brief)

Test every feature; check the official docs. These rules exist so past incidents don't repeat. Full docs: **README.md**.

## What

`gbc` (one Python app) drives **beets** via subprocess to recover a chaotic library into a clean **album**
library (any Subsonic/DLNA player). Album-mode + AcoustID: only complete, strong albums are kept; loose
singletons stay in the source for manual curation.

## Architecture

`gbc run` (manual) + `gbc inbox` (cron) call one pipeline (`passes/pipeline.py`):
**import → upgrade → albumdedup → convert → verify → acousticbrainz → qa**.

beets does art/genres/replaygain/scrub/ftintitle natively on import (`auto: yes`). gbc adds, per pass:
**artfix** (strip mime=None WMA art pre-import so scrub can't crash), **upgrade** (a better dup-skipped source
copy replaces a clean album), **albumdedup** (cross-source dup albums), **dedup** + source-shell prune (move-mode only),
**convert** (WMA→Opus, WAV/AIFF/ALAC→FLAC, hi-res FLAC >48kHz/>16bit→16-bit/≤48kHz, before verify), **verify** (AcoustID imposter → quarantine),
**acousticbrainz** (network BPM/key/mood), **qa** (audit + cull corrupt). beets via
`beets.run_beet` (captures stdout+stderr; `--pretend` → stderr). `setup.sh` is the only bash.

- **Move-vs-copy = beets' decision** (`beetscfg.py`). Source CONSUMED (move, copy+delete) → dedup + shell-prune
  run. Source PRESERVED (copy/reflink/hardlink/symlink/in-place) → source READ-ONLY, those skipped; gbc never
  mutates a preserved source (the curation backlog; `gbc singletons` dup-skips what's already clean). Album extras
  (booklet/scans/`.lrc`/`.cue`/`.log`) ride along via the **filetote** plugin in BOTH modes; fetchart owns the cover.
- **Quarantine** (`sidecars.quarantine_dir`): `<reason>/<Albumartist>/<Album (Year)>/…`. Reasons: `imposters`,
  `duplicates`, `shells`, `converted`, `downsampled`, `corrupt`, `upgraded`. Good (`upgraded`,
  `downsampled` = the hi-res masters) never mixes with bad.
- **Logs:** one append-only `$LOG_DIR/gbc.log`, lines tagged `[pass]`+run-id; beets' decisions →
  `import-decisions.log`.
- **Incremental:** verify/ab/qa scope to items added since the last good run (watermark); `--all` reprocesses,
  `--reimport` re-evaluates seen folders (`beet -I`). A killed run resumes via `BEETSDIR/gbc-run-progress.json`
  (finished passes skipped). Each pass isolates per-item (`util.skip_on_error`).
- **Tooling:** `mise run test|lint|fix|audit`; stdlib **unittest** (no pytest), no network.

## Opt-in passes (manual)

Clean top-folders sort via `_`: `_Singles/` (loose), `_Various Artists/`, `_Soundtracks/` — set in `config.yaml`
`paths:`.
- **`gbc singletons [DIR] [--apply]`** — loose source tracks + quarantined imposters → singletons in `_Singles/`;
  then Nova-first re-tag + PROMOTE any album now complete (verified vs the live MB tracklist) into a real album.
- **`gbc nova [--refresh-cache]`** — *detachable*: classify reconstructable Radio-Nova compils. Cache
  `BEETSDIR/gbc-nova-cache.json`.
- **`gbc upgrade [DIR] [--apply]`** — also runs in the pipeline. Replaces a clean album with a better source copy
  dup-skipped at import. Correlate by duration-multiset + artist + title. Lossless replaces lossy; lossy→lossy
  only on a ≥64k codec-efficiency effective-bitrate jump; WMA source excluded; already-lossless clean = cutoff.

## Config

Paths from `config.env` (copy of `config.env.example`, gitignored) — use the vars, never hardcode: `BEET`,
`BEETSDIR`, `MUSIC_SRC`, `MUSIC_CLEAN`, `MUSIC_DUMP` (never `rm`), `LOG_DIR`. `config.py` resolves `$GBC_CONFIG`
/ `~/.config/gbc/` / repo root. API keys in `beets/config.yaml` are redacted (`REPLACE_ME`) — supply your own.

## CRITICAL RULES (learned the hard way)

1. **Never delete → move to `$MUSIC_DUMP`** (an `rm` once destroyed thousands of tracks). Snapshot + `cp
   library.db` before mass ops. Only empty dir shells may be `rmdir`'d.
2. **Never bulk `modify`/`move`/`remove` without a query** (empty = whole library). Back up `library.db` first.
3. **Confirm before any irreversible op**, same turn; **test on ~10** before the whole library.
4. **Scrub is mandatory on every move.** The `mime=None` crash (WMA/ASF) → strip art FIRST via `helpers/`
   (`scan-scrub-crash.py` → `mutagen-strip.py`). Never disable scrub.
5. **Never `--from-logfile`** (parent paths + `;` → recursion). Import the directory directly.
6. **`musicbrainz` must be in `plugins:`** — without it `chroma` yields no MB candidates, fingerprinting
   silently finds nothing.
7. **Never dedup on the db alone (`mb_trackid`)** — the same recording on an album AND a compilation is legit.
   Disk-correlate by duration; keep the best-QUALITY copy (lossless > lossy, then codec-normalised bitrate;
   MB > Discogs only as an equal-quality tiebreak — `gbc/quality.py`), never break an album folder.
8. **Compilations grouped by album TITLE, not albumartist**; exclude generic titles + dominant-artist albums
   (comp-heavy ≠ comp).

## Verified facts (do not re-debate)

- **Skips ≠ rate-limiting** (0 real 429/503 over 10k+ evals); skips = weak match + quiet mode refusing to guess.
  No personal AcoustID key needed. **Never lower the match threshold** — a confident wrong-edition match slips
  past `verify`.
- **WMA** stored as "Windows Media" → query `format::Windows`, not `format:WMA`.
- **AcousticBrainz is frozen, not dead:** read API (`/api/v1/{low,high}-level?recording_ids=`, keyed by
  `mb_trackid`) still serves analysed recordings. NOT `beets-xtractor`/Essentia (CPU-heavy); NOT beets' built-in
  `acousticbrainz` plugin (deprecated; mangles `initial_key` "F# major"→"F" via its `[\W\s]+major` regex — our
  pass emits canonical "F#"/"F#m"). Keeps a curated 14-field subset: bpm+initial_key → file tags; 7 mood_* +
  danceable/tonal/key_strength + moods_mirex/voice_instrumental → flex attrs + injected to files via mutagen,
  applied by `beet modify` then a `beet write` reconciliation (never a homemade try_write). Drops genre/gender/
  timbre/rhythm/chord/average_loudness/low-level DSP.
- **VA comps matched via Discogs land `comp=False`**; import normalizes to `comp=True` (`beet modify`) so
  players don't split by artist.
- **Metadata sources: `musicbrainz discogs deezer bandcamp`** (Deezer ships with beets; Bandcamp needs
  `beetcamp`; both keyless. Spotify/Beatport out — key-gated). Additive (ranked by distance), never override a
  good MB match. More sources ≠ more recovery — the lever for loose tracks is `gbc singletons`.

## Style

Concise English, no emoji. `file:line` not pasted code. Confirm in one word. Never `git commit` without explicit approval.
