"""
CLI entrypoints. Every one of these is reachable from an OpenBot Bot ONLY
through the policy allowlist (`python -m pod.<cmd>`), which is how the shell
grant stays useful without being an escape hatch.

    python -m pod.cli validate          # offline self-test, no keys, no network
    python -m pod.cli run-one --help    # push one concept through the pipeline
    python -m pod.cli report            # P&L + funnel + guardrails
    python -m pod.cli halt "reason"     # THE KILL SWITCH
    python -m pod.cli resume "note"     # human-only restart after a halt
    python -m pod.cli funnel            # where experiments are dying
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def _setup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    sys.path.insert(0, str(Path(__file__).parent.parent))


def cmd_validate(_: argparse.Namespace) -> int:
    import subprocess
    here = Path(__file__).resolve().parents[2]
    return subprocess.call([sys.executable, str(here / "scripts" / "validate.py")])


def cmd_run_one(a: argparse.Namespace) -> int:
    from .db import open_db
    from .pipeline import Pipeline

    pipe = Pipeline(open_db())
    result = pipe.run_one(
        niche=a.niche,
        concept=a.concept,
        phrase=a.phrase,
        keywords=a.keywords.split(","),
        audience=a.audience,
        style=a.style,
        art_subject=a.art_subject or a.concept,
        subphrase=a.subphrase,
        layout=a.layout,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def cmd_report(_: argparse.Namespace) -> int:
    from .db import open_db
    from .sentinel import Sentinel

    db = open_db()
    s = Sentinel(db)
    print(s.daily_report())
    print()
    with db.conn.cursor() as cur:
        cur.execute("SELECT stage, count(*) AS n FROM experiments GROUP BY stage ORDER BY n DESC")
        print("FUNNEL:")
        for r in cur.fetchall():
            print(f"  {r['stage']:<12} {r['n']}")
        cur.execute(
            """SELECT split_part(kill_reason, ':', 1) AS gate, count(*) AS n
                 FROM experiments WHERE stage='KILLED' AND kill_reason IS NOT NULL
                 GROUP BY 1 ORDER BY n DESC LIMIT 10"""
        )
        rows = cur.fetchall()
        if rows:
            print("\nTOP KILL REASONS:")
            for r in rows:
                print(f"  {r['gate']:<28} {r['n']}")
    return 0


def cmd_halt(a: argparse.Namespace) -> int:
    from .db import open_db
    from .sentinel import Sentinel

    if not a.reason or len(a.reason) < 10:
        print("halt requires a real reason (>= 10 chars). It goes in the audit log.",
              file=sys.stderr)
        return 2
    result = Sentinel(open_db()).emergency_halt(a.reason, delist_all=a.delist_all)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    from .db import open_db
    from .sentinel import Sentinel

    result = Sentinel(open_db()).resume(a.note)
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    _setup()
    p = argparse.ArgumentParser(prog="pod")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate", help="offline self-test").set_defaults(fn=cmd_validate)

    r = sub.add_parser("run-one", help="run one concept through the full pipeline")
    r.add_argument("--niche", required=True)
    r.add_argument("--concept", required=True)
    r.add_argument("--phrase", required=True, help="exact text on the garment")
    r.add_argument("--keywords", required=True, help="comma separated")
    r.add_argument("--audience", default="")
    r.add_argument("--style", default="retro_sunset")
    r.add_argument("--art-subject", default="")
    r.add_argument("--subphrase", default="")
    r.add_argument("--layout", default="arch_top")
    r.set_defaults(fn=cmd_run_one)

    sub.add_parser("report").set_defaults(fn=cmd_report)

    h = sub.add_parser("halt", help="emergency kill switch")
    h.add_argument("reason")
    h.add_argument("--delist-all", action="store_true")
    h.set_defaults(fn=cmd_halt)

    res = sub.add_parser("resume", help="human-only restart after a halt")
    res.add_argument("note")
    res.set_defaults(fn=cmd_resume)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
