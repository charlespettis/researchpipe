"""Stage 1 — Research discovery.

Sources
  * arXiv API (cs.CR)                         https://info.arxiv.org/help/api/
  * Semantic Scholar bulk search, filtered to USENIX Security, NDSS, IEEE S&P
    and ACM CCS                               https://api.semanticscholar.org/api-docs/
  * USENIX technical-sessions pages (abstracts S2 lacks for USENIX papers)
  * OpenAlex (abstract back-fill by DOI)      https://docs.openalex.org/

DBLP would be the canonical venue index but currently blocks automated clients
behind a bot challenge, so venue membership comes from Semantic Scholar.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import date

from .models import Evidence, Paper
from . import progress
from .net import Net

VENUES = {
    "usenix": ("USENIX Security", "USENIX Security Symposium"),
    "ndss": ("NDSS", "Network and Distributed System Security Symposium"),
    "sp": ("IEEE S&P", "IEEE Symposium on Security and Privacy"),
    "ccs": ("ACM CCS", "Conference on Computer and Communications Security"),
}

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

DEFAULT_S2_QUERY = "framework | tool | system | detection | fuzzing | analysis | defense"


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


def search_arxiv(net: Net, topic: str | None, since: date, until: date, limit: int) -> list[Paper]:
    q = f"cat:cs.CR AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]"
    if topic:
        terms = " AND ".join(f'all:"{t.strip()}"' for t in topic.split(",") if t.strip())
        q += f" AND ({terms})"
    papers: list[Paper] = []
    start = 0
    while len(papers) < limit:
        page = min(100, limit - len(papers))
        xml = net.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": q,
                "start": start,
                "max_results": page,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            kind="text",
        )
        if not xml:
            break
        root = ET.fromstring(xml)
        entries = root.findall("a:entry", ATOM)
        if not entries:
            break
        for e in entries:
            abs_url = e.findtext("a:id", "", ATOM).strip()
            aid = re.sub(r"v\d+$", "", abs_url.rsplit("/abs/", 1)[-1])
            title = " ".join(e.findtext("a:title", "", ATOM).split())
            published = e.findtext("a:published", "", ATOM)[:10]
            pdf = next((l.get("href") for l in e.findall("a:link", ATOM) if l.get("title") == "pdf"), None)
            journal_ref = e.findtext("arxiv:journal_ref", "", ATOM)
            papers.append(
                Paper(
                    key=f"arxiv:{aid}",
                    title=title,
                    abstract=" ".join(e.findtext("a:summary", "", ATOM).split()),
                    year=int(published[:4]) if published else None,
                    date=published,
                    venue=_venue_from_ref(journal_ref + " " + e.findtext("arxiv:comment", "", ATOM)) or "arXiv",
                    authors=[a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
                    arxiv_id=aid,
                    doi=e.findtext("arxiv:doi", None, ATOM),
                    url=f"https://arxiv.org/abs/{aid}",
                    pdf_url=pdf,
                    arxiv_comment=" ".join(e.findtext("arxiv:comment", "", ATOM).split()),
                    sources=[Evidence(claim="Listed in arXiv cs.CR", source="arXiv API", url=f"https://arxiv.org/abs/{aid}")],
                )
            )
        start += len(entries)
        if len(entries) < page:
            break
    return papers


def _venue_from_ref(text: str) -> str | None:
    """arXiv comments often say 'Accepted at USENIX Security 2025'."""
    t = text.lower()
    if "usenix security" in t or "usenix sec" in t:
        return "USENIX Security"
    if "ndss" in t:
        return "NDSS"
    if re.search(r"\b(ieee s&p|ieee sp|oakland|symposium on security and privacy)\b", t) and "european" not in t:
        return "IEEE S&P"
    if re.search(r"\bccs\b", t) and "asia" not in t:
        return "ACM CCS"
    return None


# ---------------------------------------------------------------------------
# Semantic Scholar (venues)
# ---------------------------------------------------------------------------


def search_venue(net: Net, venue_key: str, topic: str | None, since: date, until: date, limit: int) -> list[Paper]:
    label, s2_venue = VENUES[venue_key]
    query = " + ".join(t.strip() for t in topic.split(",")) if topic else DEFAULT_S2_QUERY
    papers: list[Paper] = []
    token = None
    while len(papers) < limit:
        params = {
            "query": query,
            "venue": s2_venue,
            "publicationDateOrYear": f"{since:%Y-%m-%d}:{until:%Y-%m-%d}",
            "fields": "title,year,venue,abstract,externalIds,openAccessPdf,url,publicationDate,authors,citationCount",
            "sort": "publicationDate:desc",
        }
        if token:
            params["token"] = token
        data = net.get("https://api.semanticscholar.org/graph/v1/paper/search/bulk", params=params)
        if data is None:
            progress.get().warn(
                f"{label}: Semantic Scholar request failed — "
                f"{'results are partial' if papers else 'this venue is MISSING from the scan'}. "
                "Re-run later (successful requests are cached) or set S2_API_KEY."
            )
            break
        if not data.get("data"):
            break
        for p in data["data"]:
            ext = p.get("externalIds") or {}
            doi = ext.get("DOI")
            aid = ext.get("ArXiv")
            key = f"arxiv:{aid}" if aid else (f"doi:{doi.lower()}" if doi else f"s2:{p['paperId']}")
            papers.append(
                Paper(
                    key=key,
                    title=" ".join((p.get("title") or "").split()),
                    abstract=" ".join((p.get("abstract") or "").split()),
                    year=p.get("year"),
                    date=p.get("publicationDate"),
                    venue=label,
                    authors=[a.get("name", "") for a in (p.get("authors") or [])],
                    arxiv_id=aid,
                    doi=doi,
                    s2_id=p.get("paperId"),
                    url=f"https://doi.org/{doi}" if doi else p.get("url"),
                    pdf_url=(p.get("openAccessPdf") or {}).get("url") or None,
                    citation_count=p.get("citationCount"),
                    sources=[
                        Evidence(
                            claim=f"Published at {label} ({p.get('year')})",
                            source="Semantic Scholar venue index",
                            url=p.get("url"),
                        )
                    ],
                )
            )
            if len(papers) >= limit:
                break
        token = data.get("token")
        if not token:
            break
    return papers


# ---------------------------------------------------------------------------
# Abstract back-fill
# ---------------------------------------------------------------------------

_USENIX_ARTICLE = re.compile(
    r'<h2>\s*<a href="(/conference/usenixsecurity\d+/presentation/[^"]+)">(.*?)</a>\s*</h2>(.*?)</article>', re.S
)
_USENIX_DESC = re.compile(r"field-name-field-paper-description-long[^>]*>(.*?)</div>", re.S)


def usenix_abstracts(net: Net, years: set[int]) -> dict[str, tuple[str, str]]:
    """Title -> (abstract, presentation URL) from USENIX Security session pages."""
    index: dict[str, tuple[str, str]] = {}
    for y in sorted(years):
        page = net.get(f"https://www.usenix.org/conference/usenixsecurity{y % 100:02d}/technical-sessions", kind="text")
        if not page:
            continue
        for href, title, body in _USENIX_ARTICLE.findall(page):
            m = _USENIX_DESC.search(body)
            if not m:
                continue
            text = html.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
            index[norm_title(html.unescape(title))] = (" ".join(text.split()), "https://www.usenix.org" + href)
    return index


def openalex_abstract(net: Net, doi: str) -> str:
    params = {"select": "abstract_inverted_index"}
    if net.openalex_key:
        params["api_key"] = net.openalex_key
    w = net.get(f"https://api.openalex.org/works/doi:{doi}", params=params)
    inv = (w or {}).get("abstract_inverted_index")
    if not inv:
        return ""
    pos = sorted((i, word) for word, idxs in inv.items() for i in idxs)
    return " ".join(word for _, word in pos)


# ---------------------------------------------------------------------------
# Tooling-relevance prefilter
# ---------------------------------------------------------------------------

POSITIVE = {
    r"\bwe (present|propose|design|implement|build|introduce|develop)\b": 1.0,
    r"\b(framework|tool|toolchain|prototype|system)\b": 0.8,
    r"\bopen[- ]source(d)?\b": 1.0,
    r"\b(fuzz\w*|static analysis|symbolic|taint|sanitizer|detector|scanner|monitor\w*)\b": 0.8,
    r"\b(automat\w+|scalab\w+|practical)\b": 0.5,
    r"\b(outperform\w*|faster|speedup|improv\w+)\b": 0.7,
    r"\d+(\.\d+)?\s*(%|x\b|×|times)": 0.8,
    r"\b(cve|zero-day|previously unknown|new vulnerabilities|new bugs)\b": 0.9,
    r"\b(github\.com|artifact|available at)\b": 0.6,
}
NEGATIVE = {
    r"\b(sok|systematization|survey)\b": 1.2,
    r"\b(user study|interview\w*|we surveyed|qualitative)\b": 0.8,
    r"\b(lower bound|we prove|theorem)\b": 0.6,
    r"\bmeasurement study\b": 0.5,
}


def relevance(p: Paper) -> tuple[float, list[str]]:
    text = f"{p.title} {p.abstract} {p.arxiv_comment}".lower()
    hits: list[str] = []
    s = 0.0
    for pat, w in POSITIVE.items():
        if re.search(pat, text):
            s += w
            hits.append("+" + re.search(pat, text).group(0))
    for pat, w in NEGATIVE.items():
        if re.search(pat, text):
            s -= w
            hits.append("−" + re.search(pat, text).group(0))
    if p.peer_reviewed:
        s += 0.8
        hits.append(f"+peer-reviewed ({p.venue})")
    if not p.abstract:
        s -= 1.5
        hits.append("−no abstract")
    return max(0.0, min(1.0, s / 7.0)), hits


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def discover(
    net: Net,
    topic: str | None,
    since: date,
    until: date,
    venues: list[str],
    per_source: int,
) -> list[Paper]:
    found: list[Paper] = []
    if "arxiv" in venues:
        progress.get().source(f"arXiv cs.CR {since} → {until}")
        got = search_arxiv(net, topic, since, until, per_source)
        progress.get().info(f"arXiv: {len(got)} papers")
        found += got
    for v in venues:
        if v in VENUES:
            progress.get().source(f"{VENUES[v][0]} via Semantic Scholar")
            got = search_venue(net, v, topic, since, until, per_source)
            progress.get().info(f"{VENUES[v][0]}: {len(got)} papers")
            found += got

    # Merge duplicates by normalized title (arXiv preprint + venue version).
    merged: dict[str, Paper] = {}
    for p in found:
        k = norm_title(p.title)
        if not k:
            continue
        if k not in merged:
            merged[k] = p
            continue
        m = merged[k]
        if p.peer_reviewed and not m.peer_reviewed:
            m.venue = p.venue
        m.abstract = m.abstract or p.abstract
        m.arxiv_id = m.arxiv_id or p.arxiv_id
        m.doi = m.doi or p.doi
        m.s2_id = m.s2_id or p.s2_id
        m.pdf_url = m.pdf_url or p.pdf_url
        m.citation_count = m.citation_count if m.citation_count is not None else p.citation_count
        m.sources += p.sources
    papers = list(merged.values())

    # Back-fill missing abstracts.
    usenix_years = {p.year for p in papers if p.venue == "USENIX Security" and p.year and not p.abstract}
    if usenix_years:
        progress.get().source("Back-filling abstracts (USENIX program pages, OpenAlex)")
        idx = usenix_abstracts(net, usenix_years)
        for p in papers:
            if p.venue == "USENIX Security" and not p.abstract and norm_title(p.title) in idx:
                p.abstract, p.url = idx[norm_title(p.title)]
                p.sources.append(Evidence(claim="Abstract from USENIX program page", source="usenix.org", url=p.url))
    for p in papers:
        if not p.abstract and p.doi:
            p.abstract = openalex_abstract(net, p.doi)
            if p.abstract:
                p.sources.append(
                    Evidence(claim="Abstract from OpenAlex", source="OpenAlex", url=f"https://api.openalex.org/works/doi:{p.doi}")
                )

    for p in papers:
        p.discovery_score, _ = relevance(p)
    papers.sort(key=lambda p: p.discovery_score, reverse=True)
    return papers
