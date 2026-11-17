"""The five-stage pipeline as a callable, shared by batch CLI and interactive console."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import progress
from .discover import VENUES, discover, relevance
from .feasibility import ArtifactIndex, assess_feasibility
from .gaps import analyze_gap
from .models import Opportunity, Paper, StageResult
from .net import Net
from .novelty import Extractor, score_novelty
from .rank import DEFAULT_WEIGHTS, assess_impact, finalize
from .report import write_reports

ALL_VENUES = ["arxiv", *VENUES]


@dataclass
class RunConfig:
    topic: str | None = None
    since: date | None = None  # default: until - 365d
    until: date | None = None  # default: today
    venues: list[str] = field(default_factory=lambda: list(ALL_VENUES))
    per_source: int = 40
    top: int = 12
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    use_llm: bool = True
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    out: str | None = None
    cache_dir: str = ".cache"
    cache_hours: float = 168
    offline: bool = False

    def window(self) -> tuple[date, date]:
        until = self.until or date.today()
        return (self.since or until - timedelta(days=365)), until

    def out_dir(self) -> Path:
        _, until = self.window()
        slug = re.sub(r"[^a-z0-9]+", "-", (self.topic or "all").lower()).strip("-") or "all"
        return Path(self.out or f"reports/{slug}-{until:%Y%m%d}")

    def validate(self) -> None:
        bad = [v for v in self.venues if v not in ALL_VENUES]
        if bad:
            raise ValueError(f"unknown venue(s): {', '.join(bad)} (choose from {', '.join(ALL_VENUES)})")
        if not self.venues:
            raise ValueError("at least one venue is required")
        s, u = self.window()
        if s > u:
            raise ValueError(f"since ({s}) is after until ({u})")
        unknown = set(self.weights) - set(DEFAULT_WEIGHTS)
        if unknown:
            raise ValueError(f"unknown weight(s): {', '.join(unknown)}")

    def to_json(self) -> dict:
        d = asdict(self)
        d["since"] = str(self.since) if self.since else None
        d["until"] = str(self.until) if self.until else None
        return d


@dataclass
class RunResult:
    opportunities: list[Opportunity]
    meta: dict
    paths: dict[str, Path]
    n_discovered: int


def _discovery_stage(p: Paper, rank: int, total: int) -> StageResult:
    score, hits = relevance(p)
    return StageResult(
        score=round(score, 3),
        confidence=0.85 if p.abstract else 0.4,
        summary=f"Ranked {rank} of {total} discovered papers by tooling relevance ({p.venue}{', ' + str(p.year) if p.year else ''}).",
        method=[
            "Found via: " + "; ".join(sorted({s.source for s in p.sources})) + ".",
            "Tooling-relevance prefilter over title + abstract + arXiv comment: "
            + (", ".join(hits) if hits else "no signals")
            + f" → {score:.2f}.",
            "Only the top-ranked papers proceed to the (costlier) extraction and GitHub stages.",
        ],
        evidence=p.sources[:4],
        notes=[] if p.abstract else ["No abstract found in any source."],
    )


def run_pipeline(cfg: RunConfig, net: Net | None = None, argv: str = "") -> RunResult | None:
    cfg.validate()
    rep = progress.get()
    since, until = cfg.window()
    net = net or Net(Path(cfg.cache_dir), ttl_hours=cfg.cache_hours, offline=cfg.offline)
    if not net.github_token:
        rep.warn("No GitHub token (GITHUB_TOKEN or `gh auth login`); gap analysis will be slow and partial.")

    # 1 — discovery
    rep.stage("discover", "running", f"{cfg.topic or 'all topics'} · {since} → {until}")
    papers = discover(net, cfg.topic, since, until, cfg.venues, cfg.per_source)
    if not papers:
        rep.warn("No papers found. Try a broader topic or a wider date window.")
        rep.stage("discover", "done", "0 papers")
        return None
    selected = [p for p in papers if p.abstract][: cfg.top]
    rep.stage("discover", "done", f"{len(papers)} unique papers → analyzing top {len(selected)}")

    # 2–4 — per paper
    extractor = Extractor(net, cfg.model, cfg.effort, use_llm=cfg.use_llm)
    artifacts = ArtifactIndex(net)
    for k in ("novelty", "gap", "feasibility"):
        rep.stage(k, "running", f"0/{len(selected)}")
    opps: list[Opportunity] = []
    n = len(selected)
    for i, p in enumerate(selected, 1):
        rep.item(i, n, p.title, "novelty")
        ext, mode, note = extractor.extract(p)
        nov = score_novelty(p, ext, mode, note)
        rep.stage("novelty", "running", f"{i}/{n}")
        rep.item(i, n, p.title, "gap")
        gap, repos, issues = analyze_gap(net, p, ext, heuristic_queries=(mode != "claude"))
        rep.stage("gap", "running", f"{i}/{n}")
        rep.item(i, n, p.title, "feasibility")
        feas = assess_feasibility(p, ext, mode, [r for r in repos if r.get("official")], artifacts)
        rep.stage("feasibility", "running", f"{i}/{n}")
        imp = assess_impact(p, ext, mode, issues)
        opps.append(
            finalize(
                Opportunity(
                    paper=p,
                    extraction=ext,
                    extraction_mode=mode,
                    discovery=_discovery_stage(p, papers.index(p) + 1, len(papers)),
                    novelty=nov,
                    gap=gap,
                    feasibility=feas,
                    impact=imp,
                    related_repos=repos,
                    demand_issues=issues,
                ),
                cfg.weights,
            )
        )
    modes = {o.extraction_mode for o in opps}
    rep.stage("novelty", "done", f"{n} papers · " + ("Claude" if modes == {"claude"} else "heuristic" if modes == {"heuristic"} else "Claude + heuristic"))
    rep.stage("gap", "done", f"{sum(len(o.related_repos) for o in opps)} repos, {sum(len(o.demand_issues) for o in opps)} issues")
    rep.stage("feasibility", "done", f"{sum(1 for o in opps if any(e.source.startswith('Security Artifacts') for e in o.feasibility.evidence))} with AE badges")

    # 5 — rank + report
    rep.stage("rank", "running")
    opps.sort(key=lambda o: (o.opportunity_score, o.confidence), reverse=True)
    extraction = "Claude " + cfg.model if modes == {"claude"} else "heuristic" if modes == {"heuristic"} else "mixed (Claude + heuristic fallback)"
    meta = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "topic": cfg.topic,
        "since": str(since),
        "until": str(until),
        "venues": cfg.venues,
        "n_discovered": len(papers),
        "n_analyzed": len(opps),
        "extraction": extraction,
        "extraction_desc": (
            f"Claude ({cfg.model}, effort {cfg.effort}) fills a fixed schema from each abstract via structured outputs; "
            "quantified claims must carry verbatim quotes, which are string-checked against the abstract. Papers the model "
            "declines or that fail fall back to the regex heuristic (flagged per card)."
            if "claude" in modes
            else "Regex heuristic over each abstract (no Claude credentials or LLM disabled). Lower confidence; set "
            "ANTHROPIC_API_KEY for model-based extraction."
        ),
        "weights": cfg.weights,
        "config": cfg.to_json(),
        "argv": argv,
    }
    paths = write_reports(opps, meta, cfg.out_dir())
    rep.stage("rank", "done", str(paths["html"]))
    return RunResult(opportunities=opps, meta=meta, paths=paths, n_discovered=len(papers))
