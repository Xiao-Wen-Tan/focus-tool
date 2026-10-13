"""Convert a Focus Tool state file (version 1, 2, 3 or 4) to a complete version 4 file.

Usage: python tools/migrate_state.py IN.json OUT.json

IN is only read. OUT must not exist yet, so nothing is ever overwritten.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import focus_core as fc  # noqa: E402


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    src, out = Path(args[0]), Path(args[1])
    if out.exists() or out.resolve() == src.resolve():
        print(f"refusing to write {out}: it already exists", file=sys.stderr)
        return 1
    try:
        doc = fc.migrate_doc(json.loads(src.read_text(encoding="utf-8")))
    except (OSError, KeyError, TypeError, ValueError) as ex:
        print(f"cannot read {src}: {ex}", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "x", encoding="utf-8") as f:  # "x" fails if OUT appeared meanwhile
        f.write(json.dumps(doc, indent=2, ensure_ascii=False))
    print(f"wrote {out} (version {doc['version']}, {len(doc['plan']['tasks'])} tasks, {len(doc['done'])} done)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
