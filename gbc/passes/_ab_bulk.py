"""Bulk-apply AcousticBrainz fields to the beets library in ONE process.

The acousticbrainz pass would otherwise shell out to `beet modify` once per recording -- ~N `beet` startups,
which is the pass's real cost (the AcousticBrainz fetches themselves are NOT rate-limited). This applies the
whole batch in a single beets process instead. It is run by the BEETS venv's python (which can `import
beets`; gbc's own venv cannot), discovered from the `beet` entry point's shebang:

    <beets-python> _ab_bulk.py <library.db> <fields.json>      fields.json = {mb_trackid: {field: value}, ...}

Mirrors `beet modify` exactly: db store (incl. flex attrs) + write native tags (bpm/initial_key) to the
file. Prints the number of items updated. Imports only beets + stdlib (never gbc -- different venv).
"""
import json
import sys
from pathlib import Path

from beets.library import Library


def main() -> int:
    db_path, fields_path = sys.argv[1], sys.argv[2]
    with Path(fields_path).open(encoding="utf-8") as fh:
        data = json.load(fh)

    lib = Library(db_path)
    by_mbid: dict = {}
    for item in lib.items():                       # one DB read; index every item by its recording id
        mb = item.get("mb_trackid")
        if mb:
            by_mbid.setdefault(mb, []).append(item)

    updated = 0
    with lib.transaction():                        # one commit for the whole batch
        for mbid, fields in data.items():
            for item in by_mbid.get(mbid, []):     # a recording can sit on several albums -> several items
                for key, value in fields.items():
                    item[key] = value
                item.store()                       # db (flex attrs typed by the `types` plugin at query time)
                item.try_write()                   # bpm/initial_key -> file tags via mediafile (like `beet modify`)
                updated += 1
    print(updated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
