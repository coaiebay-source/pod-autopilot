#!/usr/bin/env python3
"""Thin wrapper: push one concept through the full pipeline.

    DRY_RUN=1 ASSET_BASE_URL=https://assets.example.test \\
    python scripts/run_one.py --niche nursing \\
        --concept "Night Shift Nurse Coffee" \\
        --phrase "STILL RUNNING ON CAFFEINE" \\
        --keywords "night shift nurse,nurse coffee,12 hour shift" \\
        --art-subject "a steaming coffee cup silhouette under a starry night sky"

Equivalent to `python -m pod.cli run-one`. Kept as a script because it is the
command you will actually type during the DRY_RUN phase.
"""
import os, subprocess, sys
from pathlib import Path

os.environ.setdefault("DRY_RUN", "1")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pod.cli import main  # noqa: E402

sys.argv = [sys.argv[0], "run-one"] + sys.argv[1:]
raise SystemExit(main())
