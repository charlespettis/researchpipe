"""Data models shared by every pipeline stage.

Every stage produces a `StageResult`: a score, a confidence, a plain-language
explanation of *how* the score was reached, and a list of `Evidence` items that
cite where each fact came from. The report renders these uniformly, so every
claim in the final output traces back to a source link.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    """One cited fact: what was observed, and where."""

    claim: str
    source: str  # human-readable source name, e.g. "arXiv abstract", "GitHub search"
    url: str | None = None


class StageResult(BaseModel):
    """Uniform output of each pipeline stage."""

    score: float = 0.0  # 0..1, higher = better for the opportunity
    confidence: float = 0.0  # 0..1, how much the evidence supports the score
    summary: str = ""  # the one-paragraph result
    method: list[str] = Field(default_factory=list)  # how the result was determined
    evidence: list[Evidence] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # caveats / missing data


class Paper(BaseModel):
    """A discovered paper, merged across sources."""

    key: str  # stable id: arxiv:<id> | doi:<doi> | s2:<id>
    title: str
    abstract: str = ""
    year: int | None = None
    date: str | None = None
    venue: str = ""  # "arXiv", "USENIX Security", "NDSS", "IEEE S&P", "ACM CCS"
    authors: list[str] = Field(default_factory=list)
    arxiv_id: str | None = None
    doi: str | None = None
    s2_id: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    arxiv_comment: str = ""
    citation_count: int | None = None
    sources: list[Evidence] = Field(default_factory=list)  # where we found it
    discovery_score: float = 0.0  # tooling-relevance prefilter score

    @property
    def peer_reviewed(self) -> bool:
        return self.venue not in ("", "arXiv")


# ---------------------------------------------------------------------------
# Structured extraction schema (what Claude fills in for Stage 2).
# extra="forbid" -> additionalProperties: false, required by structured outputs.
# ---------------------------------------------------------------------------


class Improvement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str = Field(description="What was measured, e.g. 'edge coverage', 'false positive rate'.")
    result: str = Field(description="The reported value or factor, e.g. '18x faster', '+23% coverage'.")
    baseline: str = Field(description="What it was compared against, or 'unspecified'.")
    quote: str = Field(description="The exact sentence fragment from the abstract supporting this. Copy verbatim.")


class NoveltyExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    background: str = Field(description="2-3 sentences: the security problem and why it matters, in plain language.")
    prior_limitation: str = Field(description="What existing approaches/tools cannot do or do poorly, per the paper.")
    novel_capability: str = Field(description="The specific new capability or technique the paper introduces.")
    contribution_type: Literal[
        "new_capability",
        "measurable_improvement",
        "defense_system",
        "detection_or_analysis_tool",
        "benchmark_or_dataset",
        "attack_only",
        "measurement_study",
        "theory_or_formal",
    ]
    improvements: list[Improvement] = Field(description="Quantified improvements claimed in the abstract. Empty if none.")
    novelty_strength: Literal["low", "medium", "high"]
    novelty_reason: str = Field(description="One sentence justifying novelty_strength.")
    tooling_implication: str = Field(
        description="What an open-source tool built on this would do for practitioners, and who would use it."
    )
    proposed_tool: str = Field(description="One-line description of the most useful open-source tool this enables.")
    target_users: list[str] = Field(description="e.g. 'firmware security testers', 'SOC analysts', 'CI pipelines'.")
    method_name: str = Field(description="The system/tool name the paper uses (e.g. 'ELFuzz'), or '' if none.")
    github_queries: list[str] = Field(
        description="3-5 short GitHub search queries (2-4 keywords each) that would find existing tools with this capability."
    )
    code_release_claim: str = Field(description="What the abstract says about releasing code/artifacts, or 'not stated'.")
    datasets_benchmarks: list[str] = Field(description="Datasets/benchmarks/targets named in the abstract.")
    compute_requirements: Literal["laptop", "workstation", "gpu", "llm_api", "special_hardware", "cluster", "unknown"]
    implementation_scope: Literal["S", "M", "L", "XL"] = Field(
        description="Effort for a solo developer to build a usable OSS v1: S<2wk, M<2mo, L<6mo, XL>6mo."
    )
    scope_reason: str


class Opportunity(BaseModel):
    """Final per-paper output, carrying all stage results."""

    paper: Paper
    extraction: NoveltyExtraction | None = None
    extraction_mode: Literal["claude", "heuristic"] = "heuristic"
    discovery: StageResult
    novelty: StageResult
    gap: StageResult
    feasibility: StageResult
    impact: StageResult
    opportunity_score: float = 0.0
    confidence: float = 0.0
    confidence_label: Literal["High", "Medium", "Low"] = "Low"
    opportunity_type: str = ""
    verdict: str = ""
    related_repos: list[dict] = Field(default_factory=list)
    demand_issues: list[dict] = Field(default_factory=list)
