"""Entry point.

  researchpipe                 → interactive console
  researchpipe --topic X ...   → one-shot batch run (scriptable)
"""

from __future__ import annotations

import argparse
import shlex
import sys
from datetime import date

from .pipeline import ALL_VENUES, RunConfig, run_pipeline
from .rank import DEFAULT_WEIGHTS


def parse_weights(s: str | None) -> dict[str, float]:
    w = dict(DEFAULT_WEIGHTS)
    if s:
        for part in s.split(","):
            k, v = part.split("=")
            if k.strip() not in w:
                raise ValueError(f"unknown weight '{k}'; expected one of {', '.join(w)}")
            w[k.strip()] = float(v)
    return w


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="researchpipe",
        description="Research → novelty → gap → feasibility → ranked open-source security tooling opportunities. "
        "Run with no arguments for the interactive console.",
    )
    ap.add_argument("-i", "--interactive", action="store_true", help="Open the interactive console (default with no arguments).")
    ap.add_argument("--topic", help='Topic filter; commas mean AND (e.g. "fuzzing" or "firmware,rehosting"). Omit for a general scan.')
    ap.add_argument("--since", help="Start date YYYY-MM-DD (default: 365 days ago).")
    ap.add_argument("--until", help="End date YYYY-MM-DD (default: today).")
    ap.add_argument("--venues", default=",".join(ALL_VENUES), help=f"Comma list from: {','.join(ALL_VENUES)}.")
    ap.add_argument("--per-source", type=int, default=40, help="Max papers fetched from each source (default 40).")
    ap.add_argument("--top", type=int, default=12, help="How many top-relevance papers to analyze in depth (default 12).")
    ap.add_argument("--model", default="claude-opus-5-5", help="Claude model for novelty extraction.")
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--no-llm", action="store_true", help="Skip Claude; use the heuristic extractor only.")
    ap.add_argument("--weights", help="Override ranking weights, e.g. novelty=0.4,gap=0.3,feasibility=0.2,impact=0.1")
    ap.add_argument("--out", help="Output directory (default reports/<topic>-<date>).")
    ap.add_argument("--cache-dir", default=".cache", help="HTTP/LLM cache directory (default .cache).")
    ap.add_argument("--cache-hours", type=float, default=168, help="Cache freshness in hours (default 168).")
    ap.add_argument("--offline", action="store_true", help="Use cached responses only; no network.")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    if not argv or args.interactive:
        from .tui import run_console

        return run_console()

    try:
        cfg = RunConfig(
            topic=args.topic,
            since=date.fromisoformat(args.since) if args.since else None,
            until=date.fromisoformat(args.until) if args.until else None,
            venues=[v.strip() for v in args.venues.split(",") if v.strip()],
            per_source=args.per_source,
            top=args.top,
            model=args.model,
            effort=args.effort,
            use_llm=not args.no_llm,
            weights=parse_weights(args.weights),
            out=args.out,
            cache_dir=args.cache_dir,
            cache_hours=args.cache_hours,
            offline=args.offline,
        )
        result = run_pipeline(cfg, argv="researchpipe " + shlex.join(argv))
    except ValueError as e:
        raise SystemExit(f"error: {e}")
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    if result is None:
        return 1
    print(f"Wrote {result.paths['html']}\n      {result.paths['md']}\n      {result.paths['json']}", file=sys.stderr)
    for i, o in enumerate(result.opportunities[:5], 1):
        print(f"  {i}. {o.opportunity_score:.2f} [{o.confidence_label}] {o.opportunity_type} — {o.paper.title[:80]}")
    return 0
