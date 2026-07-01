"""Drive beets via subprocess -- the canonical interface.

beets writes much of its output (incl. the `--pretend` plan) to STDERR, so by default we merge stdout+stderr,
log every line (tagged by pass), and return the full text for callers to parse. BEETSDIR is always set in the
child env.
"""
import os
import subprocess
import threading
import time

from .config import Config
from .logs import get_logger

_HEARTBEAT_S = 60       # beet is silent during long file-write/replaygain phases -> log a liveness line meanwhile


def run_beet(cfg: Config, args, *, overlay: str | None = None, passname: str,
             echo_lines: bool = True, merge_stderr: bool = True, check: bool = False) -> tuple[int, str]:
    """Run `beet [-c overlay] <args...>` -> (returncode, merged_output_text).
    echo_lines=False captures silently (for `ls`/counts/queries we parse rather than dump to the log).
    check=True RAISES RuntimeError on a nonzero exit -- use it for the scope query a post-import pass builds its
    work from: a failed scope query must abort the pass (so the pipeline HOLDS the watermark) rather than look
    like an empty scope and let the window be silently skipped."""
    log = get_logger(passname)
    cmd = [cfg.beet]
    if overlay:
        cmd += ["-c", str(cfg.overlay(overlay))]
    cmd += [str(a) for a in args]
    env = dict(os.environ, BEETSDIR=str(cfg.beetsdir))
    log.info("$ %s", " ".join(cmd))
    lines: list[str] = []
    # merge_stderr=False keeps stdout CLEAN for callers parsing structured output (e.g. `beet config` YAML,
    # which beet's stderr warnings corrupt).
    err = subprocess.STDOUT if merge_stderr else subprocess.DEVNULL
    last = time.monotonic()                         # updated on every line; the heartbeat measures silence since it
    stop = threading.Event()

    def _heartbeat() -> None:
        # in quiet import the long `manipulate_files` stage (rewriting tags into huge FLACs) emits nothing -- without
        # this the log looks frozen for many minutes. The `[passname]` tag says which pass is still working.
        while not stop.wait(_HEARTBEAT_S):
            idle = time.monotonic() - last
            if idle >= _HEARTBEAT_S:
                log.info("... still working: %ds since last beet output", int(idle))

    hb = threading.Thread(target=_heartbeat, daemon=True)
    if echo_lines:                                  # silent capture (ls/counts) is fast -> no heartbeat
        hb.start()
    try:
        # surrogateescape: non-UTF-8 file names round-trip identically (path keys stay stable across passes),
        # and a stray byte never crashes the capture.
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                              text=True, errors="surrogateescape", bufsize=1, env=env) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                last = time.monotonic()
                line = raw.rstrip("\n")
                lines.append(line)                  # returned text keeps EVERY line (incl. blanks, for parsing)
                if echo_lines and line.strip():     # the log skips blank lines only -- readability, not data
                    log.info("%s", line)
    except FileNotFoundError as e:
        raise RuntimeError(f"beet not found ({cfg.beet!r}) -- install beets or run ./setup.sh") from e
    finally:
        stop.set()
    if check and proc.returncode:
        raise RuntimeError(f"beet {' '.join(str(a) for a in args)!r} failed (rc={proc.returncode}, pass={passname})")
    return proc.returncode, "\n".join(lines)
