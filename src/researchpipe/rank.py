"""Stage 5 — Ranked project opportunities.

opportunity = 0.30·novelty + 0.30·gap + 0.25·feasibility + 0.15·impact
confidence  = the same weighted mean over each stage's own confidence

Weights are deliberately simple and printed in the report so a reader can
re-derive any score by hand; override them with --weights.
"""

from __future__ import annotations

import math

from .models import Evidence, NoveltyExtraction, Opportunity, Paper, StageResult

DEFAULT_WEIGHTS = {"novelty": 0.30, "gap": 0.30, "feasibility": 0.25, "impact": 0.15}


def assess_impact(p: Paper, ext: NoveltyExtraction, mode: str, issues: list[dict]) -> StageResult:
    demand = min(1.0, 0.2 * len(issues) + sum(i["reactions"] + i["comments"] for i in issues) / 60)
    venue = 1.0 if p.peer_reviewed else 0.55
    users = min(1.0, len(ext.target_users) / 3) if ext.target_users else 0.4
    cites = min(1.0, math.log1p(p.citation_count) / math.log1p(50)) if p.citation_count is not None else 0.3
    score = 0.35 * demand + 0.25 * venue + 0.2 * users + 0.2 * cites
    method = [
        f"Demand: {len(issues)} relevant GitHub issues requesting this capability elsewhere → {demand:.2f}.",
        f"Venue: {'peer-reviewed top-4 venue' if p.peer_reviewed else 'arXiv preprint only (not yet peer-reviewed)'} → {venue:.2f}.",
        f"Audience breadth: {len(ext.target_users)} distinct user groups → {users:.2f}.",
        f"Citations: {p.citation_count if p.citation_count is not None else 'unknown'} → {cites:.2f} (log-scaled, 50+ = 1.0).",
        f"Impact = 0.35×demand + 0.25×venue + 0.2×audience + 0.2×citations = {score:.2f}.",
    ]
    ev = [Evidence(claim=f"Target users: {', '.join(ext.target_users) or 'not identified'}", source="Stage 2 extraction")]
    ev += [Evidence(claim=s.claim, source=s.source, url=s.url) for s in p.sources[:2]]
    conf = 0.5 + (0.1 if mode == "claude" else 0) + (0.1 if p.citation_count is not None else 0)
    return StageResult(
        score=round(score, 3),
        confidence=round(conf, 3),
        summary=ext.tooling_implication,
        method=method,
        evidence=ev,
        notes=["Citation counts lag months behind publication; recent papers are under-weighted on this signal."],
    )


def finalize(o: Opportunity, weights: dict[str, float]) -> Opportunity:
    stages = {"novelty": o.novelty, "gap": o.gap, "feasibility": o.feasibility, "impact": o.impact}
    total = sum(weights.values())
    o.opportunity_score = round(sum(weights[k] * s.score for k, s in stages.items()) / total, 3)
    o.confidence = round(sum(weights[k] * s.confidence for k, s in stages.items()) / total, 3)
    o.confidence_label = "High" if o.confidence >= 0.65 else "Medium" if o.confidence >= 0.45 else "Low"

    has_official = any(r.get("official") for r in o.related_repos)
    if o.novelty.score < 0.35:
        o.opportunity_type = "Low priority — limited tooling novelty"
    elif o.gap.score <= 0.3:
        o.opportunity_type = "Contribute to an existing tool"
    elif o.gap.score >= 0.75 and has_official:
        o.opportunity_type = "Productionize a research prototype"
    elif o.gap.score >= 0.7:
        o.opportunity_type = "Greenfield tool"
    elif has_official:
        o.opportunity_type = "Harden & integrate the authors' prototype"
    else:
        o.opportunity_type = "Differentiated alternative"

    ext = o.extraction
    tool = ext.proposed_tool if ext else o.paper.title
    o.verdict = (
        f"{o.opportunity_type}: {tool} "
        f"(score {o.opportunity_score:.2f}, {o.confidence_label.lower()} confidence)."
    )
    return o
