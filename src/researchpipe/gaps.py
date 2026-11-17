"""Stage 3 — Existing-tool gap analysis (GitHub).

For each paper we search GitHub for:
  1. the paper's own implementation (arXiv id in a README, or the system name),
  2. existing tools with the same capability (Claude-generated capability queries),
  3. open issues in other projects asking for that capability (demand signal).

Each candidate repository is scored for relevance by keyword overlap with the
capability description, then classified as: official research prototype,
mature competing tool, or unrelated. The gap score follows from what exists.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import Evidence, NoveltyExtraction, Paper, StageResult
from .net import Net

PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unlicense", "0BSD", "Zlib", "BSL-1.0"}
WEAK_COPYLEFT = {"MPL-2.0", "LGPL-2.1", "LGPL-3.0", "EPL-2.0"}
COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0"}

_STOP = set(
    "a an the of for and or to in on with via by from tool tools framework system based using new open source "
    "security analysis approach detection github".split()
)


def license_class(spdx: str | None) -> str:
    if not spdx or spdx == "NOASSERTION":
        return "none/unknown"
    if spdx in PERMISSIVE:
        return "permissive"
    if spdx in WEAK_COPYLEFT:
        return "weak copyleft"
    if spdx in COPYLEFT:
        return "copyleft"
    return "other"


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9+\-]{2,}", text.lower()) if w not in _STOP}


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def _search_repos(net: Net, q: str, n: int = 8) -> list[dict] | None:
    data = net.get("https://api.github.com/search/repositories", params={"q": q, "sort": "stars", "order": "desc", "per_page": n})
    return None if data is None else data.get("items", [])


def _search_issues(net: Net, q: str, n: int = 6) -> list[dict] | None:
    data = net.get(
        "https://api.github.com/search/issues",
        params={"q": f"{q} is:issue", "sort": "reactions", "order": "desc", "per_page": n},
    )
    return None if data is None else data.get("items", [])


def analyze_gap(net: Net, p: Paper, ext: NoveltyExtraction, heuristic_queries: bool) -> tuple[StageResult, list[dict], list[dict]]:
    cap_terms = _tokens(" ".join(ext.github_queries) + " " + ext.novel_capability + " " + ext.proposed_tool)
    query_terms = _tokens(" ".join(ext.github_queries))
    name = ext.method_name.strip()

    searches: list[tuple[str, str]] = []  # (purpose, query)
    if p.arxiv_id:
        searches.append(("paper's arXiv id in README", f'"{p.arxiv_id}" in:readme'))
    if name and len(name) >= 4:
        searches.append(("system name", f'"{name}" in:name,description,readme'))
    for q in ext.github_queries[:4]:
        searches.append(("capability", q))

    repos: dict[str, dict] = {}
    failed = 0
    for purpose, q in searches:
        items = _search_repos(net, q)
        if items is None:
            failed += 1
            continue
        for it in items:
            r = repos.setdefault(
                it["full_name"],
                {
                    "name": it["full_name"],
                    "url": it["html_url"],
                    "description": (it.get("description") or "")[:240],
                    "stars": it.get("stargazers_count", 0),
                    "pushed_at": it.get("pushed_at"),
                    "archived": it.get("archived", False),
                    "license": (it.get("license") or {}).get("spdx_id"),
                    "topics": it.get("topics", []),
                    "found_by": [],
                },
            )
            r["found_by"].append(f"{purpose}: {q}")

    # Classify each repo.
    for r in repos.values():
        text_tokens = _tokens(f"{r['name'].replace('/', ' ')} {r['description']} {' '.join(r['topics'])}")
        overlap = len(text_tokens & cap_terms) / max(1, min(len(cap_terms), 6))
        r["relevance"] = round(min(1.0, overlap), 2)
        r["license_class"] = license_class(r["license"])
        r["age_days"] = _age_days(r["pushed_at"])
        by_id = any(f.startswith("paper's arXiv") for f in r["found_by"])
        by_name = bool(name) and name.lower() in r["name"].lower()
        r["official"] = by_id or (by_name and r["relevance"] >= 0.2)
        active = r["age_days"] is not None and r["age_days"] < 365 and not r["archived"]
        r["mature"] = (not r["official"]) and r["stars"] >= 300 and active and r["relevance"] >= 0.4
        if r["official"]:
            r["role"] = "paper's implementation"
        elif r["mature"]:
            r["role"] = "established tool, overlapping capability"
        elif r["relevance"] >= 0.4:
            r["role"] = "related project"
        else:
            r["role"] = "weak match"

    ranked = sorted(repos.values(), key=lambda r: (r["official"], r["mature"], r["relevance"], r["stars"]), reverse=True)
    relevant = [r for r in ranked if r["official"] or r["relevance"] >= 0.4][:10]
    official = [r for r in relevant if r["official"]]
    mature = [r for r in relevant if r["mature"]]

    # Demand signal: issues in other projects asking for this capability.
    issues: list[dict] = []
    for q in ext.github_queries[:2]:
        items = _search_issues(net, q)
        if items is None:
            failed += 1
            continue
        for it in items:
            t = _tokens(it.get("title", ""))
            rel = len(t & query_terms) / max(1, min(len(query_terms), 4))
            if rel >= 0.5:
                issues.append(
                    {
                        "title": it["title"][:160],
                        "url": it["html_url"],
                        "repo": it["repository_url"].split("/repos/", 1)[-1],
                        "state": it.get("state"),
                        "comments": it.get("comments", 0),
                        "reactions": (it.get("reactions") or {}).get("total_count", 0),
                    }
                )
    seen = set()
    issues = [i for i in issues if not (i["url"] in seen or seen.add(i["url"]))][:6]

    release_claimed = bool(re.search(r"open[- ]source|github|publicly available|release|available at", ext.code_release_claim, re.I)) and not re.search(r"not stated", ext.code_release_claim, re.I)

    # Gap score from what exists.
    best_mature = max((r["relevance"] for r in mature), default=0)
    if best_mature >= 0.6:
        score, kind = 0.25, "Established tools already cover much of this capability — best path is a plugin or upstream contribution."
    elif best_mature >= 0.4:
        score, kind = 0.5, "Adjacent established tools exist but do not clearly provide this capability."
    elif official:
        o = official[0]
        stale = o["age_days"] is None or o["age_days"] > 365 or o["archived"]
        unlicensed = o["license_class"] == "none/unknown"
        if stale or unlicensed:
            score, kind = 0.78, (
                "Only a research prototype exists ("
                + ", ".join(x for x, c in (("not updated in 12+ months", stale), ("no license", unlicensed)) if c)
                + ") — room for a maintained, reusable tool."
            )
        elif o["stars"] < 150:
            score, kind = 0.5, (
                "The authors' implementation is maintained and licensed but has little adoption — the opportunity is "
                "hardening, packaging and integration (CI, existing toolchains), not a from-scratch tool."
            )
        else:
            score, kind = 0.35, "The authors' implementation is active and adopted; a new tool would compete with it."
    elif release_claimed:
        score, kind = 0.6, (
            "No repository located, but the paper says its code is (or will be) released — likely an author "
            "implementation exists that GitHub search did not surface."
        )
    elif relevant:
        score, kind = 0.7, "Only loosely related projects found; no tool appears to implement this capability."
    else:
        score, kind = 0.9, "No implementation or comparable tool found on GitHub — greenfield."

    conf = 0.62 if not heuristic_queries else 0.48
    if p.arxiv_id:
        conf += 0.08
    if failed:
        conf -= 0.15 * min(2, failed)
    if not net.github_token:
        conf -= 0.1
    method = [
        f"Ran {len(searches)} GitHub repository searches: "
        + "; ".join(f"[{purpose}] `{q}`" for purpose, q in searches)
        + ".",
        "Relevance = overlap between each repo's name/description/topics and the capability keywords extracted in Stage 2.",
        "Classification: 'paper's implementation' if found via arXiv id or system name; 'established tool' if ≥300★, pushed "
        "within 12 months, not archived, relevance ≥0.4.",
        f"Searched issues for the top capability queries to find unmet demand in other projects ({len(issues)} relevant).",
        "Gap rule: established tool with relevance ≥0.6 → 0.25; adjacent tools → 0.5; only stale/unlicensed/low-adoption "
        "prototype → 0.78; active adopted prototype → 0.35; loose matches only → 0.7; nothing → 0.9.",
    ]
    ev = []
    for r in relevant[:6]:
        age = f"{r['age_days']}d since push" if r["age_days"] is not None else "push date unknown"
        ev.append(
            Evidence(
                claim=f"{r['role']}: {r['name']} — {r['stars']}★, {age}, license {r['license'] or 'none'} "
                f"({r['license_class']}), relevance {r['relevance']}",
                source="GitHub search",
                url=r["url"],
            )
        )
    for i in issues[:4]:
        ev.append(Evidence(claim=f"Demand: “{i['title']}” in {i['repo']} ({i['comments']} comments, {i['state']})", source="GitHub issues", url=i["url"]))
    notes = [
        "GitHub search only: tools hosted elsewhere (GitLab, vendor sites) or named very differently can be missed, so "
        "'nothing found' is weaker evidence than 'found'."
    ]
    if release_claimed and not official:
        conf -= 0.12
        notes.append("The paper states its code is released, but no matching repository was found; check the paper PDF for the link.")
    if failed:
        notes.append(f"{failed} GitHub searches failed (rate limit or network); results are partial.")
    if not net.github_token:
        notes.append("No GitHub token: unauthenticated search limits are low (10/min).")
    lic = [r for r in official if r["license_class"] in ("none/unknown", "copyleft")]
    for r in lic:
        notes.append(
            f"Reference code {r['name']} is {r['license_class']} ({r['license'] or 'no license'}): "
            + ("no reuse rights by default — a clean-room implementation from the paper is required." if r["license_class"] == "none/unknown"
               else "derived code must stay under a compatible copyleft license.")
        )
    conf = max(0.1, min(0.9, conf))
    return StageResult(score=score, confidence=round(conf, 3), summary=kind, method=method, evidence=ev, notes=notes), relevant, issues
