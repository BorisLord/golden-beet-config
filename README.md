# golden-beets-config

Turn a chaotic music library — tens of thousands of loose, mis-tagged files — into a clean **album** library
for any player (Navidrome, Jellyfin, Plex, Subsonic/DLNA). A documented [beets](https://beets.io) config for
album-mode recovery, wrapped by a small Python CLI (`gbc`) that adds the orchestration a config can't express
(dedup, sidecars, QA, format conversion, quality upgrades). Albums are matched by **AcoustID fingerprint** +
tags, so duplicate track numbers, download-batch folders and untitled rips don't matter — the folder structure
is ignored.

## The config

[`golden-beets-config.yaml`](golden-beets-config.yaml) is the standalone beets config, every setting commented:
album mode + AcoustID (identify by audio, not names), `quiet` + `quiet_fallback: skip` (auto-accept strong
matches only, never guess), complete-albums-only (`max_rec` penalises missing/extra tracks), native enrichment
on import (cover art, MusicBrainz genres, ReplayGain R128, ftintitle, scrub), `zero` junk cleaning, sane paths
(`_Various Artists/`, `_Soundtracks/`, continuous multi-disc numbering). Copy it to `~/.config/beets/config.yaml`,
set `directory:`, and it works **standalone** for ~80% of the value; the `gbc` CLI adds the rest.

## Quick start

```bash
git clone <repo-url> && cd golden-beets-config
./setup.sh     # checks deps, installs beets + the gbc CLI (via uv), deploys the config (+ optional cron)
gbc run        # the pipeline (incremental)
```

By default everything lives under `~/Music/beetsPipeline/` (`source/`, `clean/`, `quarantine/`, `logs/`). Drop
**album folders** in `source/` — matched albums go to `clean/` (moved or copied per beets `import.move`/`copy`),
the rest stay in `source/` to curate. Change paths in `config.env` (created by setup) and re-run `./setup.sh`.

## Commands

```
gbc run [--all] [--reimport]   the pipeline now; --all re-checks all, --reimport re-tries seen folders (beet -I)
gbc inbox               cron door: import a fresh drop if anything is new, then the pipeline
gbc import [SOURCE] [--reimport]   album-match import only
gbc qa [QUERY]          read-only technical audit + anomaly scan
gbc anomaly [QUERY]     read-only name/anomaly scan only
gbc verify [QUERY]      quarantine imposter tracks (audio ≠ tagged recording) via AcoustID
gbc reclaim             copy-mode: move fully-verified source albums to quarantine
gbc acousticbrainz [QUERY]   fetch BPM/key/mood from AcousticBrainz (network-only; bpm+key → file tags)
gbc convert             normalise formats in clean: WMA→Opus, WAV/AIFF/ALAC→FLAC (originals → quarantine)
gbc singletons [DIR] [--apply]   loose source tracks + imposters → singletons in _Singles/; promote complete albums (Nova-first)
gbc nova [--refresh-cache]       [detachable] classify reconstructable Radio-Nova compils
gbc upgrade [DIR] [--apply]      replace a clean album with a better dup-skipped source copy (also runs in the pipeline)
gbc init [--cron]       (re)deploy config + beets overlays (+ schedule cron)
gbc uninstall [--purge] remove the tooling (never your music)
```

**Incremental by default:** `run`/`inbox` keep a watermark, so verify/acousticbrainz/qa only touch what's new
(import is incremental via beets). `--all` re-checks everything; `--reimport` re-evaluates seen folders.

## Pipeline

    import → upgrade → albumdedup → convert → verify → acousticbrainz → qa → reclaim

| pass | what it does |
|---|---|
| **import** | AcoustID + MusicBrainz match, scrub, art/genres/ReplayGain; only complete, strong albums kept (`quiet` + `quiet_fallback: skip` — never guess) |
| **upgrade** | replace a clean album with a better source copy dup-skipped at import (lossless > lossy, or ≥64k effective-bitrate jump; old copy → quarantine, never deleted) |
| **albumdedup** | same album imported twice (MB vs Discogs) → quarantine the lesser copy (correlate by duration; keep the best quality — lossless > lossy, then codec-normalised bitrate; MB only breaks an equal-quality tie) |
| **convert** | WMA→Opus, WAV/AIFF/ALAC→FLAC, so every later pass operates on the final files |
| **verify** | re-fingerprint each track → quarantine imposters (audio ≠ tagged recording) |
| **acousticbrainz** | add BPM / key / mood / danceable (file tags + db flex attrs) |
| **qa** | audit + cull corrupt / undecodable files → quarantine |
| **reclaim** | copy-mode only: drain a source album to quarantine once every track is verified `ok` |

`library.db` is backed up before any file-moving pass; everything culled goes to `$MUSIC_DUMP` (**never `rm`**).
A killed multi-hour run resumes where it stopped (finished passes are skipped).

**Source consumed vs preserved** is beets' `import.move`/`copy` decision (read from `beet config`): in **move**
mode gbc also dedups the source, carries official sidecars into the album, and prunes empty shells; in
**copy/reflink/hardlink** mode the source is read-only and **reclaim** drains it once every track is verified
`ok`. Symlink/in-place never reclaim (clean would dangle).

## Configuration

`setup.sh` does all of this; this is reference for customising. It creates `config.env` (gitignored) from
`config.env.example` and deploys `beets/*.yaml` into `$BEETSDIR`. Edit `config.env` and re-run `./setup.sh`:

| Var | Meaning |
|---|---|
| `BEET` | beets binary (absolute path if not on `$PATH`) |
| `BEETSDIR` | beets config dir (`config.yaml` + overlays + `library.db`) |
| `MUSIC_SRC` | messy source library to import **from** |
| `MUSIC_CLEAN` | clean album destination (point your player here) |
| `MUSIC_DUMP` | quarantine — cull here, **never `rm`** |
| `LOG_DIR` | logs (default: next to `clean/`) |

API keys are optional: set `fanarttv_key`/`lastfm_key` in `$BEETSDIR/config.yaml` for online artwork + genres
(left `REPLACE_ME` otherwise). Discogs/Deezer/Bandcamp are extra match sources (Discogs needs a token; Deezer +
Bandcamp keyless). Never commit keys.

## Requirements

`./setup.sh` installs **beets** + **gbc** via **uv** and prints the OS command for the system tools:

- **uv** — installs beets + gbc.
- **fpcalc / Chromaprint** — AcoustID fingerprinting.
- **ffmpeg** — ReplayGain, the QA decode, `gbc convert`.
- **flac + mp3val** — the QA integrity check (`beet bad`).

`musicbrainz` MUST stay in the config's `plugins:` — without it `chroma` yields no MusicBrainz candidates and
matching finds nothing.

## Auto-import (cron)

Optional cron (`setup.sh` / `gbc init --cron`):

```
*/15 * * * * PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims:/usr/local/bin:/usr/bin:/bin gbc inbox >/dev/null 2>&1
```

Each tick `gbc inbox` takes the import lock (bows out if a run is in progress), skips unless there's something
**new** (it reads beet's `--pretend` plan from stderr), waits for the drop to finish copying, then runs the
pipeline. Cron and manual runs are mutually exclusive via the shared lock.

## Development

```bash
mise install     # python, uv, ruff
mise run test    # unit tests (stdlib unittest, no network)
mise run lint    # ruff
mise run fix     # ruff safe auto-fixes
mise run audit   # pip-audit on runtime deps
```

App in `gbc/` (CLI `cli.py`, passes in `gbc/passes/`, beets driven via subprocess in `beets.py`). The live
MusicBrainz/AcoustID match is exercised manually.

## Uninstall

`./uninstall.sh` (or `gbc uninstall`) removes the **tooling only** — cron entry, logs, `config.env`; `--purge`
also removes the beets config dir + `library.db`. It **never touches your music**.

## Gotchas (verified in production)

- **Quarantine, never `rm`** — an accidental delete once destroyed thousands of tracks. Snapshot first if your
  FS supports it (ZFS/btrfs/LVM); only empty shells may be `rmdir`'d. (Passes back up `library.db` automatically.)
- **Scrub crashes on `mime=None` images** (WMA/ASF): strip them first with `helpers/` (`scan-scrub-crash.py` to
  find, then `mutagen-strip.py`, via `uv run --with mediafile --with mutagen python helpers/<x>.py`). Never disable scrub.
- **Never dedup on the db alone** — the same recording can be on a studio album AND a compilation; correlate
  the files by duration, keep the best-quality copy (lossless > lossy, then codec-normalised bitrate), never
  break an album.
- **Never `--from-logfile`** (parent paths + `;` cause recursion) — import the directory directly.
- **WMA is stored as "Windows Media"** → query `format::Windows`, not `format:WMA`.
- **`incremental` remembers skipped folders too** — re-run `gbc run --reimport` (`-I`) to retry a re-tagged folder.
- **Skips ≠ rate-limiting** (0 real 429/503 over 10k+ evals) — skips = weak match + quiet mode refusing to
  guess. No personal AcoustID key needed.
