# researchpipe

A pipeline that turns recent security research into ranked ideas for **new open-source security tools**.

```
Research discovery ─► Technical novelty ─► Existing-tool gap ─► Reproducibility & ─► Ranked
(arXiv · USENIX ·      extraction           analysis             feasibility          opportunities
 NDSS · S&P · CCS)     (what's new?)        (GitHub)             (code · data ·
                                                                  compute · scope)
```

For every paper it analyzes, the report shows each stage's **result**, **how it was determined** (the exact
queries, rules and formulas), **evidence with source links**, and **caveats**. It also gives the background of
the problem, the limitation of prior work, what is new, what that means for open-source tooling, and a
**confidence** label for the opportunity.

## Install

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/charlespettis/researchpipe.git
cd researchpipe
uv sync
```

## Interactive console

```bash
uv run researchpipe
```

```
╭─────────────────────────────────────────────────────╮
│ ✻ researchpipe                                      │
│   security research → open-source tool opportunities│
│ Claude            ● claude-opus-5-5 · effort medium │
│ GitHub            ● gh CLI login                    │
╰─────────────────────────────────────────────────────╯
› firmware fuzzing                ← any text = scan that topic
› /scan                           ← general scan, no topic
› /show 3                         ← explain opportunity 3, every stage, with sources
› /show 3 gap                     ← just one stage
› /open 3                         ← HTML report in the browser, at #3
› /set window 6m   /set top 20   /set venues usenix,ndss,sp,ccs   /set llm off
› /runs   /load 2                 ← browse earlier reports
```

The scan shows a live panel that ticks through the five stages. Tab completes commands, settings and opportunity
numbers, and the bottom bar shows the current window, venues and mode. Settings are saved to `.researchpipe.json`.
Ctrl-C cancels a scan without losing what has already been fetched, because everything is cached. Ctrl-D exits.

## Quick start (batch / scripting)

Passing any flag runs once without the console:

```bash
uv sync
export ANTHROPIC_API_KEY=...        # optional, enables Claude-based novelty extraction
gh auth login                       # optional, reuses your GitHub login for gap analysis (or set GITHUB_TOKEN)

uv run researchpipe --topic fuzzing --since 2025-06-01
uv run researchpipe --topic "firmware,rehosting" --top 20
uv run researchpipe --venues usenix,ndss,sp,ccs --per-source 60          # peer-reviewed only, all topics
uv run researchpipe --topic "supply chain" --no-llm                       # no API key needed
```

Output goes to `reports/<topic>-<date>/`:

| File | Contents |
|---|---|
| `report.html` | Ranked table + one card per opportunity with all five stages (open in a browser) |
| `report.md` | The same content as Markdown |
| `opportunities.json` | Machine-readable output, including every repo and issue considered |

## The five stages

| Stage | Sources | What it decides | Score |
|---|---|---|---|
| **1 · Discovery** | arXiv API (`cat:cs.CR`), Semantic Scholar venue search (USENIX Security, NDSS, IEEE S&P, ACM CCS); abstracts back-filled from USENIX program pages and OpenAlex | Which papers look like they introduce a *tool-shaped* contribution | Keyword prefilter: system/framework language, quantified results, CVEs, open-source claims, peer review (+); SoK/survey/user study/proofs (−) |
| **2 · Novelty** | The abstract | Background, prior limitation, new capability, contribution type, quantified improvements, proposed tool, target users, GitHub search queries, compute class, scope | strength × contribution-type weight + bonus per *verified* quantitative claim |
| **3 · Gap** | GitHub repository and issue search | Does the paper's own code exist? Do established tools already do this? Is anyone asking for it? | Explicit rules: established overlapping tool → 0.25 … nothing found → 0.9 |
| **4 · Feasibility** | [Security Artifacts](https://secartifacts.github.io) AE badges, code links, author repo license, benchmarks, compute, scope | Can one developer realistically build it? | 0.35·code + 0.15·data + 0.25·compute + 0.25·scope |
| **5 · Ranking** | Stages 2–4 + demand (issues), venue, audience, citations | Final ordering and opportunity type | 0.30·novelty + 0.30·gap + 0.25·feasibility + 0.15·impact |

**Opportunity types:** *Greenfield tool* · *Productionize a research prototype* (author code is stale or unlicensed) ·
*Harden & integrate the authors' prototype* · *Differentiated alternative* · *Contribute to an existing tool* ·
*Low priority*.

**Confidence** is the same weighted mean over each stage's own confidence. It measures how well the evidence supports
the score, not how good the idea is. It drops when the abstract is missing, Claude's quoted claims can't be found
verbatim in the abstract, extraction ran in heuristic mode, GitHub searches failed, or the paper claims a code
release that couldn't be located. High ≥ 0.65, Medium ≥ 0.45.

## How novelty extraction works

With Anthropic credentials, Claude (`claude-opus-5-5` by default, effort `medium`) fills a fixed Pydantic schema
using structured outputs. Every claimed improvement has to come with a verbatim quote, and the code string-matches
each quote against the abstract; quotes that don't match lower confidence and are flagged in the report. Abstracts
are passed as data, not instructions. Server-side refusal fallbacks are enabled, because security abstracts can
trigger safety classifiers. If a request is declined or fails anyway, that paper falls back to the regex heuristic
and the card says so. Results are cached per paper, model, effort and prompt version, so re-runs don't cost
anything.

Without credentials, or with `--no-llm`, the heuristic extractor runs instead and confidence is lower.

## Options

```
--topic TEXT          commas mean AND ("firmware,rehosting"); omit for all of cs.CR
--since / --until     date window (default: last 365 days)
--venues LIST         arxiv,usenix,ndss,sp,ccs (default all)
--per-source N        papers fetched per source (default 40)
--top N               papers analyzed in depth (default 12)
--model / --effort    Claude model and effort for extraction
--no-llm              heuristic extraction only
--weights k=v,...     e.g. novelty=0.4,gap=0.3,feasibility=0.2,impact=0.1
--out DIR             output directory
--cache-dir / --cache-hours / --offline
```

Optional environment variables: `S2_API_KEY` (Semantic Scholar; without it the shared pool sometimes returns 429s,
which the tool retries), `OPENALEX_API_KEY`, `GITHUB_TOKEN`.

## Limitations

- Decisions come from abstracts and metadata, not full papers.
- GitHub search misses tools hosted elsewhere or named very differently. "Nothing found" is weaker evidence than
  "found".
- DBLP blocks automated clients, so venue membership comes from Semantic Scholar's venue field.
- ACM CCS artifact badges live in the ACM Digital Library and are not checked.
- Scores rank candidates for a human to review. They are not a substitute for reading the paper.
