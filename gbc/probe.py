"""Shared media-probe cache. Reading each file's tags/duration/bitrate is the expensive part of the three
pre-import source passes (dedup, sidecars.snapshot, upgrade) -- previously each walked the whole source and
probed independently (dedup even probed twice per file). Probe once via mediafile, key on path+mtime+size,
and persist: the same file is never re-read within a run NOR across runs (unchanged files are free)."""
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Probe:
    title: str       # raw tag title (callers casefold for grouping)
    length: int      # rounded seconds
    bitrate: int     # kbps
    artist: str      # albumartist or artist
    album: str
    year: str
    ext: str         # lowercased suffix incl. dot


def _fresh(key: str) -> bool:
    """True iff `key`'s file still exists with the same mtime+size (key = 'path:mtime:size'; path may hold ':')."""
    path, mt, sz = key.rsplit(":", 2)
    try:
        st = Path(path).stat()
    except OSError:
        return False
    return int(st.st_mtime) == int(mt) and st.st_size == int(sz)


class ProbeCache:
    """path+mtime+size -> Probe. A cached `False` is the "unreadable" sentinel (never re-probed). `path=None`
    -> in-memory only (no load, no save) for standalone/test callers."""

    def __init__(self, path):
        self.path = Path(path) if path is not None else None
        self._dirty = False
        self._seen = set()                                 # keys touched this run -> fresh by construction (no re-stat)
        self._c = {}
        if self.path is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}
            if isinstance(loaded, dict):                   # a corrupted non-dict cache (e.g. `[]`) -> start fresh
                self._c = loaded

    def get(self, fpath):
        try:
            st = Path(fpath).stat()
        except OSError:
            return None
        key = f"{fpath}:{int(st.st_mtime)}:{st.st_size}"   # mtime+size flip on a re-tag/re-encode -> auto re-probe
        self._seen.add(key)                                # this key == the file's CURRENT state -> fresh this run
        if key in self._c:
            v = self._c[key]
            return Probe(**v) if v else None               # dict -> Probe; False sentinel -> None
        v = self._read(fpath)
        self._c[key] = v or False
        self._dirty = True
        return Probe(**v) if v else None

    @staticmethod
    def _read(fpath):
        from mediafile import MediaFile
        try:
            mf = MediaFile(str(fpath))
        except Exception:                                  # one unreadable file never aborts a pass
            return None
        return {"title": (mf.title or "").strip(),
                "length": round(mf.length) if mf.length else 0,
                "bitrate": (mf.bitrate // 1000) if mf.bitrate else 0,
                "artist": (mf.albumartist or mf.artist or "").strip(),
                "album": (mf.album or "").strip(),
                "year": str(mf.year or "").strip(),
                "ext": Path(fpath).suffix.lower()}

    def save(self):
        if not (self._dirty and self.path is not None):
            return
        from .util import write_json
        # bound the cache: keep entries touched this run (fresh by construction) + any still valid on disk; drop
        # keys whose file is gone or superseded by a re-tag/re-encode -> no unbounded growth across runs.
        live = {k: v for k, v in self._c.items() if k in self._seen or _fresh(k)}
        write_json(self.path, live)                        # atomic tmp+replace
