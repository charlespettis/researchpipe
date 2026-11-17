"""Interactive console for researchpipe.

A prompt-driven shell: type a topic to scan, or use slash commands to view
results, drill into an opportunity, change settings and open reports.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from . import progress
from .cli import parse_weights
from .models import Opportunity, StageResult
from .pipeline import ALL_VENUES, RunConfig, RunResult, run_pipeline
from .rank import DEFAULT_WEIGHTS

ACCENT = "#d97757"
SETTINGS_FILE = Path(".researchpipe.json")
HISTORY_FILE = Path(".cache/console_history")

COMMANDS: dict[str, tuple[str, str]] = {
    "/scan": ("[topic]", "Run the pipeline; no topic = general scan of all security research"),
    "/results": ("", "Ranked table of the current results"),
    "/show": ("<n> [stage]", "Explain opportunity n: background, every stage, evidence, sources"),
    "/open": ("[n]", "Open the HTML report in your browser (optionally at opportunity n)"),
    "/set": ("<key> <value>", "Change a setting — e.g. /set window 6m, /set top 20, /set llm off"),
    "/settings": ("", "Show current settings"),
    "/runs": ("", "List previous reports in ./reports"),
    "/load": ("<n|path>", "Load a previous report's results"),
    "/clear": ("", "Clear the screen"),
    "/help": ("", "Show commands and tips"),
    "/quit": ("", "Exit (also Ctrl-D)"),
}
SET_KEYS: dict[str, str] = {
    "window": "look-back shorthand: 90d, 6m, 1y, 18m",
    "since": "start date YYYY-MM-DD (or 'default')",
    "until": "end date YYYY-MM-DD (or 'today')",
    "venues": f"comma list or 'all' — {','.join(ALL_VENUES)}",
    "top": "papers analyzed in depth",
    "per-source": "papers fetched from each source",
    "llm": "on | off — Claude novelty extraction",
    "model": "Claude model id",
    "effort": "low | medium | high | xhigh | max",
    "weights": "novelty=0.3,gap=0.3,feasibility=0.25,impact=0.15 or 'default'",
    "offline": "on | off — cached data only",
}
STAGE_KEYS = {"discovery": "discovery", "novelty": "novelty", "gap": "gap", "feasibility": "feasibility", "impact": "impact"}
CONF_STYLE = {"High": "green", "Medium": "yellow", "Low": "red"}


# ---------------------------------------------------------------------------
# Live progress
# ---------------------------------------------------------------------------


class LiveReporter(progress.Reporter):
    def __init__(self, console: Console):
        self.console = console
        self.status = {k: "pending" for k, _ in progress.STAGES}
        self.detail = {k: "" for k, _ in progress.STAGES}
        self.spinners = {k: Spinner("dots", style=ACCENT) for k, _ in progress.STAGES}
        self.current = ""
        self.waiting = ""
        self.start = time.time()
        self.warnings: list[str] = []
        self.finished: float | None = None

    def stage(self, key, status, detail=""):
        self.status[key] = status
        self.detail[key] = detail
        if status == "done" and key == "discover":
            self.current = ""
        if status == "done" and key == "rank":
            self.current = ""
            self.finished = time.time()
        self.waiting = ""

    def source(self, text):
        self.current = text
        self.waiting = ""

    def item(self, i, n, title, step):
        verb = {"novelty": "extracting novelty", "gap": "searching GitHub", "feasibility": "checking artifacts"}[step]
        self.current = f"({i}/{n}) {title[:70]} · {verb}"
        self.waiting = ""

    def info(self, msg):
        self.console.print(Text(f"  ✓ {msg}", style="dim"))

    def wait(self, msg):
        self.waiting = msg

    def warn(self, msg):
        self.warnings.append(msg)
        self.console.print(Text(f"  ⚠ {msg}", style="yellow"))

    def __rich__(self):
        rows = []
        for key, label in progress.STAGES:
            st = self.status[key]
            if st == "running":
                icon = self.spinners[key]
                lab = Text(label, style="bold")
            elif st == "done":
                icon = Text("✔", style="green")
                lab = Text(label)
            else:
                icon = Text("○", style="dim")
                lab = Text(label, style="dim")
            t = Table.grid(padding=(0, 1))
            t.add_column(width=2)
            t.add_column()
            line = lab.copy()
            if self.detail[key]:
                line.append(f"  {self.detail[key]}", style="dim")
            t.add_row(icon, line)
            rows.append(t)
        foot = Text()
        if self.current:
            foot.append(f"  ↳ {self.current}\n", style="cyan")
        if self.waiting:
            foot.append(f"  … {self.waiting}\n", style="dim yellow")
        if self.finished:
            foot.append(f"  done in {self.finished - self.start:.0f}s", style="dim")
            title = "[bold]scan complete[/]"
        else:
            foot.append(f"  {time.time() - self.start:4.0f}s · Ctrl-C to cancel", style="dim")
            title = "[bold]scanning[/]"
        return Panel(Group(*rows, foot), border_style=ACCENT, title=title, title_align="left", padding=(0, 1))


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------


class ConsoleCompleter(Completer):
    def __init__(self, app: "App"):
        self.app = app

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        parts = text.split(" ")
        if len(parts) == 1:
            for cmd, (args, desc) in COMMANDS.items():
                if cmd.startswith(parts[0]):
                    yield Completion(cmd, start_position=-len(parts[0]), display=f"{cmd} {args}".strip(), display_meta=desc)
            return
        cmd, cur = parts[0], parts[-1]
        if cmd == "/set" and len(parts) == 2:
            for k, desc in SET_KEYS.items():
                if k.startswith(cur):
                    yield Completion(k, start_position=-len(cur), display_meta=desc)
        elif cmd == "/set" and len(parts) == 3:
            opts = {
                "llm": ["on", "off"], "offline": ["on", "off"], "effort": ["low", "medium", "high", "xhigh", "max"],
                "window": ["90d", "6m", "1y", "18m", "2y"], "venues": ["all", "arxiv", "usenix,ndss,sp,ccs"],
                "weights": ["default"], "since": ["default"], "until": ["today"],
            }.get(parts[1], [])
            for o in opts:
                if o.startswith(cur):
                    yield Completion(o, start_position=-len(cur))
        elif cmd in ("/show", "/open") and len(parts) == 2 and self.app.result:
            for i, o in enumerate(self.app.result.opportunities, 1):
                if str(i).startswith(cur):
                    yield Completion(str(i), start_position=-len(cur), display_meta=o.paper.title[:60])
        elif cmd == "/show" and len(parts) == 3:
            for s in STAGE_KEYS:
                if s.startswith(cur):
                    yield Completion(s, start_position=-len(cur))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class App:
    def __init__(self):
        self.console = Console(highlight=False)
        self.cfg = self._load_settings()
        self.result: RunResult | None = None
        self.loaded_from: str | None = None

    # -- settings ---------------------------------------------------------
    def _load_settings(self) -> RunConfig:
        cfg = RunConfig()
        try:
            d = json.loads(SETTINGS_FILE.read_text())
            for k, v in d.items():
                if k in ("since", "until"):
                    v = date.fromisoformat(v) if v else None
                if hasattr(cfg, k) and k not in ("topic", "out"):
                    setattr(cfg, k, v)
        except (OSError, ValueError):
            pass
        return cfg

    def _save_settings(self) -> None:
        d = self.cfg.to_json()
        d.pop("topic", None)
        d.pop("out", None)
        SETTINGS_FILE.write_text(json.dumps(d, indent=2))

    # -- environment ------------------------------------------------------
    @staticmethod
    def _claude_configured() -> bool:
        return bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or (Path.home() / ".config/anthropic").exists()
        )

    def _github(self) -> str:
        from .net import _gh_cli_token

        if os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
            return "token from environment"
        return "gh CLI login" if _gh_cli_token() else ""

    # -- rendering --------------------------------------------------------
    def banner(self) -> None:
        c = self.console
        gh = self._github()
        claude = self._claude_configured()
        g = Table.grid(padding=(0, 2))
        g.add_column(style="dim", no_wrap=True)
        g.add_column()

        def dot(ok: bool, text: str) -> Text:
            return Text.assemble(("● " if ok else "○ ", "green" if ok else "yellow"), text)

        if not self.cfg.use_llm:
            g.add_row("Claude", dot(False, "off (/set llm on) — heuristic extraction"))
        else:
            g.add_row("Claude", dot(claude, f"{self.cfg.model} · effort {self.cfg.effort}" if claude else "no credentials — heuristic extraction (set ANTHROPIC_API_KEY)"))
        g.add_row("GitHub", dot(bool(gh), gh or "unauthenticated — slow, partial gap analysis (run gh auth login)"))
        g.add_row("Semantic Scholar", dot(bool(os.environ.get("S2_API_KEY")), "API key" if os.environ.get("S2_API_KEY") else "shared pool — expect brief rate-limit waits"))
        g.add_row("cwd", Text(str(Path.cwd()).replace(str(Path.home()), "~")))
        head = Text.assemble(("✻ ", ACCENT), ("researchpipe", "bold"), ("\n  security research → open-source tool opportunities\n", "dim"))
        c.print(Panel(Group(head, g), border_style=ACCENT, padding=(0, 1), expand=False))
        c.print(
            Text.assemble(
                ("  Type a topic to scan ", "dim"), ("(e.g. ", "dim"), ("firmware fuzzing", "bold"), (")", "dim"),
                (" · ", "dim"), ("/scan", "bold"), (" for a general scan · ", "dim"), ("/help", "bold"),
                (" · Tab completes commands\n", "dim"),
            )
        )

    def toolbar(self):
        since, until = self.cfg.window()
        venues = "all venues" if set(self.cfg.venues) == set(ALL_VENUES) else ",".join(self.cfg.venues)
        llm = "claude" if (self.cfg.use_llm and self._claude_configured()) else "heuristic"
        last = ""
        if self.result:
            t = self.result.meta.get("topic") or "general scan"
            last = f" │ results: {escape_html(t)} ({len(self.result.opportunities)})"
        return HTML(
            f" <b>{since} → {'today' if until == date.today() else until}</b> │ {venues} │ top {self.cfg.top} │ "
            f"{self.cfg.per_source}/source │ {llm}{' │ offline' if self.cfg.offline else ''}{last}"
        )

    def show_help(self) -> None:
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column(style=f"bold {ACCENT}", no_wrap=True)
        t.add_column(style="dim", no_wrap=True)
        t.add_column()
        for cmd, (args, desc) in COMMANDS.items():
            t.add_row(cmd, args, desc)
        self.console.print(Padding(t, (1, 0, 0, 2)))
        s = Table(show_header=False, box=None, padding=(0, 2), title="  /set keys", title_justify="left", title_style="bold")
        s.add_column(style="bold", no_wrap=True)
        s.add_column(style="dim")
        for k, d in SET_KEYS.items():
            s.add_row(k, d)
        self.console.print(Padding(s, (1, 0, 0, 2)))
        self.console.print(
            Padding(
                Text(
                    "Anything not starting with / is treated as a topic to scan. Commas mean AND: "
                    "\"firmware, rehosting\". Results are cached for a week, so re-running is fast.",
                    style="dim",
                ),
                (1, 2),
            )
        )

    def show_settings(self) -> None:
        since, until = self.cfg.window()
        t = Table(show_header=False, box=None, padding=(0, 2))
        t.add_column(style="bold", no_wrap=True)
        t.add_column()
        rows = [
            ("window", f"{since} → {until}" + ("" if self.cfg.since else "  (default: last 365 days)")),
            ("venues", ", ".join(self.cfg.venues)),
            ("top", f"{self.cfg.top} papers analyzed in depth"),
            ("per-source", f"{self.cfg.per_source} papers fetched per source"),
            ("llm", ("on" if self.cfg.use_llm else "off") + ("" if self._claude_configured() else "  (no credentials found → heuristic)")),
            ("model / effort", f"{self.cfg.model} / {self.cfg.effort}"),
            ("weights", ", ".join(f"{k} {v}" for k, v in self.cfg.weights.items())),
            ("offline", "on" if self.cfg.offline else "off"),
            ("saved to", str(SETTINGS_FILE)),
        ]
        for k, v in rows:
            t.add_row(k, v)
        self.console.print(Padding(t, (1, 2)))

    def show_results(self) -> None:
        if not self.result:
            self.console.print("  [dim]No results yet — type a topic or /scan.[/]")
            return
        r = self.result
        m = r.meta
        self.console.print(
            Text.assemble(
                ("\n  ", ""), (m.get("topic") or "General scan", "bold"),
                (f"  ·  {m['since']} → {m['until']}  ·  {m['n_discovered']} discovered, {m['n_analyzed']} analyzed  ·  extraction: {m['extraction']}", "dim"),
            )
        )
        t = Table(box=None, padding=(0, 1), header_style="dim", pad_edge=False)
        t.add_column("#", justify="right", style="dim")
        t.add_column("score", no_wrap=True)
        t.add_column("opportunity", ratio=3)
        t.add_column("type", style="cyan", ratio=1)
        t.add_column("conf", no_wrap=True)
        for i, o in enumerate(r.opportunities, 1):
            bar = "█" * round(o.opportunity_score * 8)
            score = Text.assemble((f"{o.opportunity_score:.2f} ", "bold"), (bar.ljust(8, "░"), ACCENT))
            title = Text.assemble((o.paper.title, ""), (f"\n{o.paper.venue} {o.paper.year or ''}", "dim"))
            if o.extraction and o.extraction.proposed_tool and o.extraction_mode == "claude":
                title.append(f"\n→ {o.extraction.proposed_tool}", style="italic dim")
            t.add_row(str(i), score, title, o.opportunity_type, Text(o.confidence_label, style=CONF_STYLE[o.confidence_label]))
            t.add_row("", "", "", "", "")
        self.console.print(Padding(t, (1, 2, 0, 2)))
        self.console.print(Text.assemble(("  /show 1", "bold"), (" explain an opportunity · ", "dim"), ("/open", "bold"), (" HTML report · ", "dim"), (str(r.paths.get("html", "")), "dim")))

    def _stage_block(self, title: str, s: StageResult) -> Group:
        bar = "█" * round(s.score * 10)
        head = Text.assemble(
            (title, f"bold {ACCENT}"), ("   score ", "dim"), (f"{s.score:.2f} ", "bold"), (bar.ljust(10, "░"), ACCENT),
            ("   confidence ", "dim"), (f"{s.confidence:.2f}", "bold"),
        )
        parts: list = [head, Text.assemble(("Result  ", "bold"), s.summary)]
        if s.method:
            parts.append(Text("How it was determined", style="bold"))
            parts.append(_bullets([Text(m, style="dim") for m in s.method]))
        if s.evidence:
            parts.append(Text("Evidence & sources", style="bold"))
            items = []
            for e in s.evidence:
                src = f"[link={e.url}]{escape(e.source)}[/link]" if e.url else escape(e.source)
                t = Text.from_markup(f"{escape(e.claim)} [dim]— {src}[/dim]")
                if e.url:
                    t.append(f"\n{e.url}", style="dim blue")
                items.append(t)
            parts.append(_bullets(items))
        if s.notes:
            parts.append(_bullets([Text(n, style="yellow") for n in s.notes], mark="⚠", style="yellow"))
        parts.append(Text(""))
        return Group(*parts)

    def show_opportunity(self, n: int, only: str | None = None) -> None:
        if not self.result:
            self.console.print("  [dim]No results yet.[/]")
            return
        if not 1 <= n <= len(self.result.opportunities):
            self.console.print(f"  [red]No opportunity {n}[/] — choose 1–{len(self.result.opportunities)}.")
            return
        o: Opportunity = self.result.opportunities[n - 1]
        p, x = o.paper, o.extraction
        links = []
        if p.url:
            links.append(f"[link={p.url}]paper[/link]")
        if p.arxiv_id:
            links.append(f"[link=https://arxiv.org/abs/{p.arxiv_id}]arXiv:{p.arxiv_id}[/link]")
        if p.pdf_url:
            links.append(f"[link={p.pdf_url}]PDF[/link]")
        authors = ", ".join(p.authors[:3]) + (" et al." if len(p.authors) > 3 else "")

        stages = [
            ("discovery", "1 · Discovery", o.discovery),
            ("novelty", "2 · Technical novelty", o.novelty),
            ("gap", "3 · Existing-tool gap", o.gap),
            ("feasibility", "4 · Reproducibility & feasibility", o.feasibility),
            ("impact", "5 · Impact & demand", o.impact),
        ]
        if only:
            stages = [s for s in stages if s[0] == only]
            if not stages:
                self.console.print(f"  [red]Unknown stage '{only}'[/] — one of {', '.join(STAGE_KEYS)}.")
                return

        head = Group(
            Text.from_markup(f"[dim]#{n} · {escape(p.venue)} {p.year or ''} · {escape(authors)}[/]"),
            Text(p.title, style="bold"),
            Text.from_markup("[dim]" + " · ".join(links) + "[/]") if links else Text(""),
            Text(""),
            Text.assemble(
                (f"{o.opportunity_type}", "bold cyan"), ("  ·  score ", "dim"), (f"{o.opportunity_score:.2f}", "bold"),
                ("  ·  confidence ", "dim"), (o.confidence_label, CONF_STYLE[o.confidence_label]), (f" ({o.confidence:.2f})", "dim"),
            ),
            Text(f"→ {x.proposed_tool}" if x else "", style="italic"),
        )
        self.console.print(Panel(head, border_style=ACCENT, padding=(0, 1)))

        if not only and x:
            g = Table.grid(padding=(0, 2))
            g.add_column(style=f"bold {ACCENT}", no_wrap=True, width=20)
            g.add_column()
            g.add_row("Background", x.background)
            g.add_row("Prior limitation", x.prior_limitation)
            g.add_row("What is new", x.novel_capability)
            g.add_row("Implication", x.tooling_implication + (f"\n[dim]Users: {escape(', '.join(x.target_users))}[/]" if x.target_users else ""))
            self.console.print(Padding(g, (0, 2, 1, 2)))
        for _, title, s in stages:
            self.console.print(Padding(self._stage_block(title, s), (0, 2)))
        if not only:
            self.console.print(
                Padding(
                    Text(
                        f"Overall confidence {o.confidence:.2f} ({o.confidence_label}) = weighted mean of the stage confidences. "
                        "It reflects how well the evidence supports the score, not how good the idea is.",
                        style="dim",
                    ),
                    (0, 2, 1, 2),
                )
            )
            self.console.print(Text.assemble(("  Tip: ", "dim"), (f"/show {n} gap", "bold"), (" shows one stage · ", "dim"), (f"/open {n}", "bold"), (" opens it in the browser", "dim")))

    # -- actions ----------------------------------------------------------
    def scan(self, topic: str | None) -> None:
        topic = (topic or "").strip() or None
        if topic and topic.lower() in ("all", "*", "general"):
            topic = None
        self.cfg.topic = topic
        if self.cfg.use_llm and not self._claude_configured():
            self.console.print("  [yellow]⚠ No Anthropic credentials found — using heuristic extraction (lower confidence).[/]")
        rep = LiveReporter(self.console)
        old = progress.get()
        progress.set_reporter(rep)
        self.console.print()
        try:
            with Live(rep, console=self.console, refresh_per_second=12, transient=False):
                result = run_pipeline(self.cfg, argv=f"researchpipe (console) topic={topic!r}")
        except KeyboardInterrupt:
            self.console.print("  [yellow]Scan cancelled.[/] Fetched data is cached, so re-running resumes quickly.")
            return
        except ValueError as e:
            self.console.print(f"  [red]{escape(str(e))}[/]")
            return
        finally:
            progress.set_reporter(old)
        if result:
            self.result = result
            self.loaded_from = None
            self.show_results()

    def open_report(self, n: int | None) -> None:
        if not self.result or "html" not in self.result.paths:
            self.console.print("  [dim]No report yet.[/]")
            return
        url = Path(self.result.paths["html"]).resolve().as_uri() + (f"#opp-{n}" if n else "")
        webbrowser.open(url)
        self.console.print(f"  [dim]Opened {url}[/]")

    def _runs(self) -> list[Path]:
        return sorted(Path("reports").glob("*/opportunities.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    def list_runs(self) -> None:
        runs = self._runs()
        if not runs:
            self.console.print("  [dim]No saved reports in ./reports yet.[/]")
            return
        t = Table(box=None, padding=(0, 2), header_style="dim")
        t.add_column("#", justify="right", style="dim")
        t.add_column("report")
        t.add_column("topic")
        t.add_column("results", justify="right")
        t.add_column("generated", style="dim")
        for i, p in enumerate(runs, 1):
            try:
                meta = json.loads(p.read_text())["meta"]
            except (OSError, ValueError, KeyError):
                continue
            t.add_row(str(i), p.parent.name, meta.get("topic") or "general scan", str(meta.get("n_analyzed", "?")), meta.get("generated", ""))
        self.console.print(Padding(t, (1, 2)))
        self.console.print("  [dim]/load <n> to view one[/]")

    def load(self, arg: str) -> None:
        runs = self._runs()
        path = runs[int(arg) - 1] if arg.isdigit() and 0 < int(arg) <= len(runs) else Path(arg)
        if path.is_dir():
            path = path / "opportunities.json"
        try:
            d = json.loads(path.read_text())
            opps = [Opportunity.model_validate(o) for o in d["opportunities"]]
        except (OSError, ValueError, KeyError) as e:
            self.console.print(f"  [red]Could not load {escape(str(path))}: {escape(str(e))}[/]")
            return
        paths = {"html": path.parent / "report.html", "md": path.parent / "report.md", "json": path}
        self.result = RunResult(opportunities=opps, meta=d["meta"], paths=paths, n_discovered=d["meta"].get("n_discovered", 0))
        self.loaded_from = str(path)
        self.show_results()

    def set(self, key: str, value: str) -> None:
        c = self.cfg
        try:
            if key == "window":
                m = re.fullmatch(r"(\d+)\s*([dwmy])", value.lower())
                if not m:
                    raise ValueError("use e.g. 90d, 8w, 6m, 1y")
                n, u = int(m.group(1)), m.group(2)
                days = n * {"d": 1, "w": 7, "m": 30, "y": 365}[u]
                c.until, c.since = None, date.today() - timedelta(days=days)
            elif key == "since":
                c.since = None if value in ("default", "none") else date.fromisoformat(value)
            elif key == "until":
                c.until = None if value in ("today", "default", "none") else date.fromisoformat(value)
            elif key == "venues":
                c.venues = list(ALL_VENUES) if value == "all" else [v.strip() for v in value.split(",") if v.strip()]
            elif key == "top":
                c.top = max(1, int(value))
            elif key == "per-source":
                c.per_source = max(1, int(value))
            elif key == "llm":
                c.use_llm = value.lower() in ("on", "true", "yes", "1")
            elif key == "offline":
                c.offline = value.lower() in ("on", "true", "yes", "1")
            elif key == "model":
                c.model = value
            elif key == "effort":
                if value not in ("low", "medium", "high", "xhigh", "max"):
                    raise ValueError("effort must be low, medium, high, xhigh or max")
                c.effort = value
            elif key == "weights":
                c.weights = dict(DEFAULT_WEIGHTS) if value == "default" else parse_weights(value)
            else:
                raise ValueError(f"unknown setting '{key}' — one of: {', '.join(SET_KEYS)}")
            c.validate()
        except ValueError as e:
            self.console.print(f"  [red]{escape(str(e))}[/]")
            return
        self._save_settings()
        self.console.print(f"  [green]✔[/] {key} = [bold]{escape(value)}[/]")

    # -- loop -------------------------------------------------------------
    def handle(self, line: str) -> bool:
        """Returns False to exit."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            self.scan(line)
            return True
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/quit", "/exit", "/q"):
            return False
        elif cmd == "/help":
            self.show_help()
        elif cmd == "/scan":
            self.scan(" ".join(args))
        elif cmd in ("/results", "/r"):
            self.show_results()
        elif cmd == "/show":
            if not args or not args[0].isdigit():
                self.console.print("  [dim]Usage: /show <n> [discovery|novelty|gap|feasibility|impact][/]")
            else:
                self.show_opportunity(int(args[0]), args[1].lower() if len(args) > 1 else None)
        elif cmd == "/open":
            self.open_report(int(args[0]) if args and args[0].isdigit() else None)
        elif cmd == "/set":
            if len(args) < 2:
                self.console.print("  [dim]Usage: /set <key> <value> — keys: " + ", ".join(SET_KEYS) + "[/]")
            else:
                self.set(args[0].lower(), " ".join(args[1:]))
        elif cmd in ("/settings", "/config"):
            self.show_settings()
        elif cmd == "/runs":
            self.list_runs()
        elif cmd == "/load":
            if args:
                self.load(args[0])
            else:
                self.console.print("  [dim]Usage: /load <n|path> — see /runs[/]")
        elif cmd == "/clear":
            self.console.clear()
            self.banner()
        else:
            self.console.print(f"  [red]Unknown command {escape(cmd)}[/] — /help lists commands.")
        return True

    def run(self) -> int:
        self.banner()
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            history=FileHistory(str(HISTORY_FILE)),
            completer=ConsoleCompleter(self),
            complete_while_typing=True,
            bottom_toolbar=self.toolbar,
            style=Style.from_dict({"prompt": f"{ACCENT} bold", "bottom-toolbar": "noreverse #888888"}),
        )
        while True:
            try:
                line = session.prompt([("class:prompt", "› ")])
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            try:
                if not self.handle(line):
                    break
            except KeyboardInterrupt:
                self.console.print("  [yellow]Cancelled.[/]")
        self.console.print("  [dim]bye[/]")
        return 0


def _bullets(items: list, mark: str = "•", style: str = "dim") -> Table:
    """Bulleted list with hanging indent, so wrapped lines align under the text."""
    t = Table.grid(padding=(0, 1))
    t.add_column(width=3, justify="right", style=style)
    t.add_column(ratio=1)
    for it in items:
        t.add_row(mark, it)
    return t


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def run_console() -> int:
    return App().run()
