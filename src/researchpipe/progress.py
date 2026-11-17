"""Progress reporting hook.

Pipeline code reports through the module-level `reporter`. The default writes
plain lines to stderr (batch mode); the interactive console swaps in a
reporter that drives a live display.
"""

from __future__ import annotations

import sys

STAGES = [
    ("discover", "Research discovery"),
    ("novelty", "Novelty extraction"),
    ("gap", "Existing-tool gap"),
    ("feasibility", "Reproducibility & feasibility"),
    ("rank", "Ranking & report"),
]


class Reporter:
    def stage(self, key: str, status: str, detail: str = "") -> None:
        """status: 'running' | 'done' | 'skipped'."""
        if status == "running":
            n = [k for k, _ in STAGES].index(key) + 1
            print(f"[{n}/5] {dict(STAGES)[key]}{' — ' + detail if detail else ''}", file=sys.stderr)

    def source(self, text: str) -> None:
        print(f"      {text}", file=sys.stderr)

    def item(self, i: int, n: int, title: str, step: str) -> None:
        if step == "novelty":
            print(f"      ({i}/{n}) {title[:90]}", file=sys.stderr)

    def info(self, msg: str) -> None:
        print(f"      {msg}", file=sys.stderr)

    def wait(self, msg: str) -> None:
        print(f"  … {msg}", file=sys.stderr)

    def warn(self, msg: str) -> None:
        print(f"  ! {msg}", file=sys.stderr)


reporter: Reporter = Reporter()


def set_reporter(r: Reporter) -> None:
    global reporter
    reporter = r


def get() -> Reporter:
    return reporter
