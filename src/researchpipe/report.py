"""Report rendering: HTML (primary), Markdown, and JSON."""

from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .models import Opportunity, StageResult


def write_reports(opps: list[Opportunity], meta: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=PackageLoader("researchpipe", "templates"), autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("report.html.j2").render(opps=opps, meta=meta)
    paths = {
        "html": out_dir / "report.html",
        "md": out_dir / "report.md",
        "json": out_dir / "opportunities.json",
    }
    paths["html"].write_text(html)
    paths["md"].write_text(to_markdown(opps, meta))
    paths["json"].write_text(json.dumps({"meta": meta, "opportunities": [o.model_dump() for o in opps]}, indent=2, default=str))
    return paths


def _stage_md(title: str, s: StageResult) -> list[str]:
    lines = [f"#### {title} — score {s.score:.2f}, confidence {s.confidence:.2f}", "", f"**Result:** {s.summary}", "", "**How determined:**"]
    lines += [f"- {m}" for m in s.method]
    if s.evidence:
        lines += ["", "**Evidence:**"]
        lines += [f"- {e.claim} — {f'[{e.source}]({e.url})' if e.url else e.source}" for e in s.evidence]
    lines += [f"> ⚠ {n}" for n in s.notes]
    return lines + [""]


def to_markdown(opps: list[Opportunity], meta: dict) -> str:
    L = [
        "# Security Tooling Opportunities",
        "",
        f"Generated {meta['generated']} · topic: **{meta['topic'] or 'all'}** · window {meta['since']} → {meta['until']} · "
        f"{meta['n_discovered']} discovered, {meta['n_analyzed']} analyzed · extraction: {meta['extraction']}",
        "",
        "Pipeline: Discovery → Novelty extraction → Existing-tool gap → Reproducibility & feasibility → Ranking",
        "",
        "| # | Paper | Venue | Type | Score | Nov | Gap | Feas | Impact | Confidence |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, o in enumerate(opps, 1):
        L.append(
            f"| {i} | {o.paper.title} | {o.paper.venue} {o.paper.year or ''} | {o.opportunity_type} | {o.opportunity_score:.2f} | "
            f"{o.novelty.score:.2f} | {o.gap.score:.2f} | {o.feasibility.score:.2f} | {o.impact.score:.2f} | {o.confidence_label} ({o.confidence:.2f}) |"
        )
    for i, o in enumerate(opps, 1):
        p, x = o.paper, o.extraction
        links = [f"[paper]({p.url})" if p.url else "", f"[arXiv:{p.arxiv_id}](https://arxiv.org/abs/{p.arxiv_id})" if p.arxiv_id else "", f"[PDF]({p.pdf_url})" if p.pdf_url else ""]
        L += [
            "",
            "---",
            f"## {i}. {p.title}",
            f"{p.venue} {p.year or ''} · {' · '.join(l for l in links if l)}",
            "",
            f"**{o.opportunity_type}** — {x.proposed_tool if x else ''} (score {o.opportunity_score:.2f}, {o.confidence_label} confidence {o.confidence:.2f})",
            "",
            f"- **Background:** {x.background if x else ''}",
            f"- **Prior limitation:** {x.prior_limitation if x else ''}",
            f"- **What is new:** {x.novel_capability if x else ''}",
            f"- **Implication:** {x.tooling_implication if x else ''}",
            "",
        ]
        for title, s in [("1 · Discovery", o.discovery), ("2 · Technical novelty", o.novelty), ("3 · Existing-tool gap", o.gap), ("4 · Reproducibility & feasibility", o.feasibility), ("5 · Impact & demand", o.impact)]:
            L += _stage_md(title, s)
    w = meta["weights"]
    L += ["---", "", f"Ranking: opportunity = {' + '.join(f'{v}·{k}' for k, v in w.items())}. Confidence = same-weighted mean of stage confidences (High ≥0.65, Medium ≥0.45).", ""]
    return "\n".join(L)
