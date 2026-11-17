"""Stage 4 — Reproducibility and feasibility.

Signals
  * Artifact-evaluation badges from the Security Artifacts registry
    (https://secartifacts.github.io — USENIX Security, NDSS, IEEE S&P, ACSAC…).
  * Code links in the abstract / arXiv comment (GitHub, GitLab, Zenodo, Figshare, HF).
  * The paper's own repository found in Stage 3, and its license.
  * Datasets/benchmarks named in the abstract (public benchmark or not).
  * Compute class and implementation scope from Stage 2.
"""

from __future__ import annotations

import re

import yaml

from .discover import norm_title
from .models import Evidence, NoveltyExtraction, Paper, StageResult
from .net import Net

AE_CONFS = {"USENIX Security": "usenixsec", "NDSS": "ndss", "IEEE S&P": "sp"}
PUBLIC_BENCHMARKS = {
    "fuzzbench", "magma", "lava-m", "juliet", "sard", "big-vul", "devign", "cicids", "unsw-nb15", "defects4j",
    "oss-fuzz", "cgc", "darpa cgc", "spec", "ember", "sorel", "virusshare", "androzoo", "drebin", "cve", "nvd",
    "primevul", "cvefixes", "secbench", "crossvul", "tranco", "common crawl", "linux kernel", "syzbot",
}
CODE_LINK = re.compile(r"https?://(?:www\.)?(github\.com|gitlab\.com|zenodo\.org|figshare\.com|huggingface\.co|bitbucket\.org)/[^\s)\]}>,;]+", re.I)

COMPUTE_SCORE = {"laptop": 1.0, "workstation": 0.9, "llm_api": 0.75, "unknown": 0.7, "gpu": 0.6, "cluster": 0.4, "special_hardware": 0.35}
SCOPE_SCORE = {"S": 1.0, "M": 0.8, "L": 0.55, "XL": 0.3}
SCOPE_TEXT = {"S": "< 2 weeks", "M": "< 2 months", "L": "< 6 months", "XL": "> 6 months"}


class ArtifactIndex:
    """Lazy title → artifact-entry index over secartifacts.github.io results pages."""

    def __init__(self, net: Net):
        self.net = net
        self.loaded: set[str] = set()
        self.index: dict[str, dict] = {}

    def lookup(self, p: Paper) -> dict | None:
        prefix = AE_CONFS.get(p.venue)
        if not prefix or not p.year:
            return None
        for y in (p.year, p.year + 1):  # AE cycles can be labelled by the next year
            conf = f"{prefix}{y}"
            if conf not in self.loaded:
                self._load(conf)
        return self.index.get(norm_title(p.title))

    def _load(self, conf: str) -> None:
        self.loaded.add(conf)
        url = f"https://raw.githubusercontent.com/secartifacts/secartifacts.github.io/main/_conferences/{conf}/results.md"
        text = self.net.get(url, kind="text")
        if not text or not text.startswith("---"):
            return
        try:
            front = yaml.safe_load(text.split("---", 2)[1]) or {}
        except yaml.YAMLError:
            return
        for a in front.get("artifacts") or []:
            if a and a.get("title"):
                a["_conf"] = conf
                a["_page"] = f"https://secartifacts.github.io/{conf}/results"
                self.index[norm_title(str(a["title"]))] = a


def assess_feasibility(
    p: Paper, ext: NoveltyExtraction, mode: str, official_repos: list[dict], artifacts: ArtifactIndex
) -> StageResult:
    ev: list[Evidence] = []
    method: list[str] = []
    notes: list[str] = []

    # --- code availability -------------------------------------------------
    ae = artifacts.lookup(p)
    links = sorted(set(m.group(0).rstrip(".") for m in CODE_LINK.finditer(f"{p.abstract} {p.arxiv_comment}")))
    official = official_repos[0] if official_repos else None

    if ae:
        badges = str(ae.get("badges", ""))
        code = 1.0 if "Reproduced" in badges else 0.9 if "Functional" in badges else 0.8
        art_url = ae.get("repository_url") or ae.get("artifact_url") or ae["_page"]
        ev.append(Evidence(claim=f"Artifact evaluation: {badges.replace('Badges: ', '')}", source=f"Security Artifacts registry ({ae['_conf']})", url=ae["_page"]))
        if art_url != ae["_page"]:
            ev.append(Evidence(claim="Evaluated artifact", source="artifact link", url=art_url))
        code_txt = f"artifact-evaluated ({badges.replace('Badges: ', '')})"
    elif official:
        code = 0.8 if official["license_class"] in ("permissive", "weak copyleft") else 0.65 if official["license_class"] == "copyleft" else 0.55
        code_txt = f"author repo {official['name']} ({official['license'] or 'no license'})"
        ev.append(Evidence(claim=f"Author implementation: {official['name']} ({official['stars']}★, license {official['license'] or 'none'})", source="GitHub", url=official["url"]))
    elif links:
        code = 0.7
        code_txt = "code link given in paper metadata"
        for l in links[:3]:
            ev.append(Evidence(claim="Code/artifact link in abstract or arXiv comment", source="paper metadata", url=l))
    elif re.search(r"open[- ]source|github|publicly available|release", ext.code_release_claim, re.I) and "not stated" not in ext.code_release_claim:
        code = 0.6
        code_txt = "paper says code is released, but the repository was not located automatically"
    else:
        code = 0.35
        code_txt = "no public code found — must reimplement from the paper"
    method.append(
        "Code availability: checked the Security Artifacts registry (AE badges), the paper's own repo from Stage 3, and code "
        f"links in the abstract/arXiv comment → {code_txt} (score {code:.2f})."
    )

    # --- data ---------------------------------------------------------------
    named = list(dict.fromkeys(d.strip() for d in ext.datasets_benchmarks if d.strip()))
    public = [d for d in named if any(b in d.lower() for b in PUBLIC_BENCHMARKS)]
    if public:
        data = 0.9
        data_txt = f"uses public benchmark(s): {', '.join(public[:4])}"
    elif named:
        data = 0.6
        data_txt = f"names datasets/targets not recognized as public benchmarks: {', '.join(named[:4])}"
    else:
        data = 0.55
        data_txt = "no datasets or benchmarks named in the abstract"
    method.append(f"Data: {data_txt} (score {data:.2f}).")

    # --- compute & scope ----------------------------------------------------
    comp = COMPUTE_SCORE[ext.compute_requirements]
    scope = SCOPE_SCORE[ext.implementation_scope]
    method.append(f"Compute class '{ext.compute_requirements}' → {comp:.2f}.")
    method.append(f"Implementation scope {ext.implementation_scope} ({SCOPE_TEXT[ext.implementation_scope]} for one developer) → {scope:.2f}. {ext.scope_reason}")

    score = 0.35 * code + 0.15 * data + 0.25 * comp + 0.25 * scope
    method.append(f"Feasibility = 0.35×code + 0.15×data + 0.25×compute + 0.25×scope = {score:.2f}.")

    conf = 0.5 + (0.15 if (ae or official) else 0) + (0.1 if mode == "claude" else 0) - (0.1 if ext.compute_requirements == "unknown" else 0)
    if ext.code_release_claim and ext.code_release_claim != "not stated":
        ev.append(Evidence(claim=f"Paper states: “{ext.code_release_claim[:200]}”", source="abstract", url=p.url))
    if p.venue not in AE_CONFS:
        notes.append(f"No artifact-evaluation registry is checked for {p.venue or 'this venue'} (ACM CCS badges live in the ACM DL).")
    if ext.compute_requirements == "special_hardware":
        notes.append("Needs specialised hardware: contributors and CI will have limited ability to reproduce results.")

    summary = f"{code_txt.capitalize()}; {data_txt}; compute: {ext.compute_requirements}; scope {ext.implementation_scope} ({SCOPE_TEXT[ext.implementation_scope]})."
    return StageResult(score=round(score, 3), confidence=round(min(0.9, conf), 3), summary=summary, method=method, evidence=ev, notes=notes)
