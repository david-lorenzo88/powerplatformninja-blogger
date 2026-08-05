"""Command line entry points.

    ppn doctor                     check configuration and connectivity
    ppn suggest                    run the topic discovery crew
    ppn suggest --explore          sweep the whole web, approve the sites, then rank
    ppn write --index 1            research → source-check → write → validate → publish
    ppn run                        suggest, then write the top-ranked topic
    ppn wp check                   verify the WordPress connection
    ppn wp push drafts/x.md        push an existing local draft to WordPress
    ppn wp preview drafts/x.md     print the Gutenberg markup without publishing

Add --dry-run to any pipeline command to execute the full graph against the
offline stub client (no Azure, no API keys, no network).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import storage
from .models import Draft, TopicSuggestion
from .settings import CONFIG_DIR, get_settings
from .util import setup_logging

app = typer.Typer(add_completion=False, help="Power Platform Ninja blogging crew.")
wp_app = typer.Typer(help="WordPress operations.")
app.add_typer(wp_app, name="wp")
config_app = typer.Typer(help="Configuration store operations (server database).")
app.add_typer(config_app, name="config")
console = Console()


# ---------------------------------------------------------------------------
# Live progress
# ---------------------------------------------------------------------------

PHASE_LABELS = {
    "scout_dispatcher": "Briefing the scouts",
    "news_scout": "News scout — searching the open web",
    "feed_scout": "Feed scout — reading curated feeds",
    "docs_scout": "Docs scout — mining Microsoft Learn",
    "scout_aggregator": "Merging scout findings",
    "source_harvester": "Collecting every site the scouts read",
    "scout_replay": "Replaying the approved sources",
    "topic_editor": "Topic editor — ranking and de-duplicating (the slow step)",
    "topic_publisher": "Writing the shortlist",
    "brief_builder": "Building the research brief",
    "dossier_entry": "Loading saved research",
    "researcher": "Researcher — gathering evidence",
    "dossier_gate": "Filing the dossier",
    "source_checker": "Source checker — verifying every citation",
    "source_gate": "Weighing the source verdict",
    "writer": "Writer — drafting",
    "draft_gate": "Handing the draft to the validators",
    "content_validator": "Content validator — reviewing",
    "design_validator": "Design validator — reviewing",
    "review_gate": "Merging validator verdicts",
    "finalizer": "Saving, cover art and publishing",
    "translator": "Translator — localising the approved draft",
    "translation_gate": "Publishing the translation",
}


class LiveProgress:
    """Spinner with an elapsed-time counter and the current pipeline phase.

    Model calls produce no events while they run, so without this a long
    generation is indistinguishable from a hang. The elapsed counter keeps
    ticking regardless of whether the workflow is emitting anything.
    """

    def __init__(self, label: str = "Starting") -> None:
        self.label = label
        self._started = time.monotonic()
        self._status = None
        self._ticker: asyncio.Task | None = None

    def _text(self) -> str:
        minutes, seconds = divmod(int(time.monotonic() - self._started), 60)
        return f"[bold]{self.label}[/]  [dim]{minutes}m{seconds:02d}s elapsed[/]"

    async def __aenter__(self) -> LiveProgress:
        self._status = console.status(self._text(), spinner="dots")
        self._status.start()
        self._ticker = asyncio.create_task(self._tick())
        return self

    async def _tick(self) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                if self._status is not None:
                    self._status.update(self._text())
        except asyncio.CancelledError:
            pass

    async def __aexit__(self, *_exc: object) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        if self._status is not None:
            self._status.stop()

    def on_event(self, event: object) -> None:
        executor_id = getattr(event, "executor_id", None)
        if executor_id and executor_id in PHASE_LABELS:
            self.label = PHASE_LABELS[executor_id]


def _run_with_progress(make_coro, *, timeout_minutes: int, what: str):
    """Run a pipeline coroutine under a spinner and a wall-clock ceiling."""

    async def runner():
        async with LiveProgress() as progress:
            try:
                return await asyncio.wait_for(
                    make_coro(progress.on_event), timeout=timeout_minutes * 60
                )
            except TimeoutError:
                console.print(
                    f"\n[red]Timed out[/] after {timeout_minutes} minutes during: "
                    f"[bold]{progress.label}[/]\n\n"
                    "This usually means one model call stalled or the context grew too "
                    "large for the deployment. Things to try, cheapest first:\n"
                    "  • Re-run — transient stalls are common.\n"
                    "  • Lower [bold]suggestions_per_run[/] in config/topics.yaml, or trim "
                    "[bold]watch_areas[/] — fewer signals means a smaller final generation.\n"
                    "  • Raise the ceiling: "
                    f"[bold]PPN_{what.upper()}_TIMEOUT_MINUTES={timeout_minutes * 2}[/]\n"
                    "  • Run with [bold]PPN_LOG_LEVEL=DEBUG[/] to see the framework's own trace."
                )
                raise typer.Exit(1) from None

    return asyncio.run(runner())


def _clients(dry_run: bool):
    if dry_run:
        from .testing import stub_clients

        console.print(
            "[yellow]DRY RUN[/] — offline stub client. No model is called and nothing is "
            "searched; the output below is fixed sample data used to exercise the graph. "
            "Drop [bold]--dry-run[/] for real results."
        )
        return stub_clients()
    from .clients import default_clients

    return default_clients()


def _load_notes(notes_path: Path | None, slug: str) -> str:
    """Author notes text for a post.

    ``--notes`` points anywhere; otherwise the default location is
    ``input/notes/<slug>.md``. A missing file is not an error — it simply means
    the post runs in analysis mode.
    """
    from .settings import ROOT

    path = notes_path if notes_path else (ROOT / "input" / "notes" / f"{slug}.md")
    if path and Path(path).exists():
        text = Path(path).read_text(encoding="utf-8")
        console.print(f"[green]Author notes[/] loaded from {path}")
        return text
    if notes_path:
        raise typer.BadParameter(f"No notes file at {notes_path}")
    return ""


# ---------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Check that configuration, credentials and endpoints are usable."""
    setup_logging()
    settings = get_settings()
    table = Table(title="ppn doctor", show_lines=False)
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    def row(name: str, ok: bool, detail: str) -> None:
        table.add_row(name, "[green]ok[/]" if ok else "[red]missing[/]", detail)

    f = settings.foundry
    row("Foundry endpoint", bool(f.project_endpoint), f.project_endpoint or "set FOUNDRY_PROJECT_ENDPOINT")
    row("Foundry model", bool(f.model), f"{f.model} (fast: {f.fast_model or f.model})")
    row("Azure credential", True, f"mode={f.credential_mode}")

    s = settings.search
    row("Web search", s.is_configured, s.status_detail)

    w = settings.wordpress
    row("WordPress", w.is_configured, f"{w.api_base} as {w.username or '<unset>'} "
                                      f"(status={w.default_status}, auto_push={w.auto_push})")

    c = settings.cover
    row(
        "Cover images",
        c.is_configured,
        f"{c.status_detail} (enabled={c.enabled}, upload={c.upload_to_wordpress})",
    )
    t = settings.translation
    tp = settings.translation_profile
    table.add_row(
        "Translation",
        "[green]on by default[/]" if t.enabled else "[dim]opt-in[/]",
        f"{settings.language} → {tp.get('target_code', 'es')} "
        f"({tp.get('target_language', '?')}). "
        + (
            "Runs on every approved draft."
            if t.enabled
            else "Off unless you pass --translate, or run `ppn translate <draft.md>`."
        ),
    )
    row("Language", True, f"drafting in {settings.language} — "
                          f"{len(settings.structure)} structure conventions loaded")

    row("Watch areas", bool(settings.watch_areas), ", ".join(a["id"] for a in settings.watch_areas))
    row("Feeds", bool(settings.feeds), f"{len(settings.feeds)} configured")
    row("Rules", bool(settings.all_rules()), f"{len(settings.all_rules())} rules loaded")
    console.print(table)

    if w.is_configured:
        from .wordpress import WordPressClient, WordPressError

        try:
            me = asyncio.run(WordPressClient().verify_connection())
            console.print(f"[green]WordPress reachable[/] — authenticated as "
                          f"{me.get('name')} (id {me.get('id')})")
        except WordPressError as exc:
            console.print(f"[red]WordPress check failed:[/] {exc}")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]WordPress check failed:[/] {exc}")


# ---------------------------------------------------------------------------


@app.command()
def suggest(
    instruction: str = typer.Option(
        "Find what is worth writing about on the Power Platform Ninja blog right now.",
        "--instruction",
        "-i",
        help="Steer the scouts, e.g. 'focus on governance changes this month'.",
    ),
    explore: bool = typer.Option(
        False,
        "--explore",
        help="Search the whole web, then approve the sites it read before any topic is proposed.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="With --explore, approve every site found at its suggested tier without prompting.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use the offline stub client."),
) -> None:
    """Scan news, feeds and Microsoft Learn, then propose ranked blog topics."""
    setup_logging()
    from .workflows import discover_topics

    clients = _clients(dry_run)
    if explore:
        result = _explore_and_shortlist(instruction, clients, approve_all=yes)
    else:
        result = _run_with_progress(
            lambda on_event: discover_topics(instruction, clients=clients, on_event=on_event),
            timeout_minutes=get_settings().run.suggest_timeout_minutes,
            what="suggest",
        )

    table = Table(title=f"Topic suggestions — {result.generated_on}")
    table.add_column("#", width=3)
    table.add_column("Score", width=6)
    table.add_column("Title", overflow="fold")
    table.add_column("Area", width=16)
    table.add_column("Format", width=14)
    for i, s in enumerate(result.suggestions, 1):
        table.add_row(str(i), f"{s.score:.0f}", s.title, s.watch_area, s.post_format)
    console.print(table)
    console.print(f"Saved to [bold]{get_settings().run.topics_dir}[/]. "
                  f"Write one with: [bold]ppn write --index 1[/]")


def _explore_and_shortlist(instruction: str, clients, *, approve_all: bool):
    """The two halves of an exploration run, with the operator in between.

    The server splits these across two runs because a worker cannot be held open
    for a human. The CLI has no such problem: one process, one prompt, and the
    approved sites are written straight back into ``config/sources.yaml``.
    """
    from .sources import merge_into_yaml_text
    from .workflows import explore_sources, shortlist_from_sources

    settings = get_settings()
    review = _run_with_progress(
        lambda on_event: explore_sources(instruction, clients=clients, on_event=on_event),
        timeout_minutes=settings.run.suggest_timeout_minutes,
        what="suggest",
    )
    if not review.candidates:
        console.print("[red]The sweep found no usable sources.[/]")
        raise typer.Exit(1)

    decisions = _approve_sources(review, approve_all=approve_all)
    approved = [d.domain for d in decisions if d.approved]
    if not approved:
        console.print("[yellow]No sources approved — nothing to build a shortlist from.[/]")
        raise typer.Exit(1)

    # Count before the write: afterwards every approved site is "known".
    known = _known_domains()
    newly_trusted = sum(1 for d in decisions if d.approved and d.domain not in known)
    path = CONFIG_DIR / "sources.yaml"
    merged = merge_into_yaml_text(path.read_text(encoding="utf-8"), decisions)
    path.write_text(merged, encoding="utf-8")
    console.print(
        f"[green]Wrote {path}[/] — {len(approved)} approved"
        + (f", {newly_trusted} newly trusted" if newly_trusted else "")
    )

    return _run_with_progress(
        lambda on_event: shortlist_from_sources(
            review.reports, approved, instruction=instruction, clients=clients, on_event=on_event
        ),
        timeout_minutes=settings.run.suggest_timeout_minutes,
        what="suggest",
    )


def _known_domains() -> set[str]:
    return {
        str(domain).lower()
        for tier in get_settings().trust_tiers.values()
        for domain in tier.get("domains", [])
    }


def _approve_sources(review, *, approve_all: bool):
    """Show what the scouts read and take the operator's verdict, site by site."""
    from .models import SourceDecision

    candidates = review.candidates
    # Sites already carrying a tier start approved; a site nobody has vetted has
    # to be said yes to explicitly. --yes approves the lot.
    chosen = {c.domain: (True if approve_all else c.known) for c in candidates}
    tiers = {c.domain: c.suggested_tier for c in candidates}

    def render() -> None:
        table = Table(title=f"Sites the scouts read — {review.generated_on}")
        table.add_column("#", width=3)
        table.add_column("✓", width=2)
        table.add_column("Site", overflow="fold")
        table.add_column("Status", width=10)
        table.add_column("Tier", width=20)
        table.add_column("Items", width=5, justify="right")
        for i, c in enumerate(candidates, 1):
            table.add_row(
                str(i),
                "[green]✓[/]" if chosen[c.domain] else " ",
                f"{c.domain}\n[dim]{c.name}[/]" if c.name != c.domain else c.domain,
                "[yellow]new[/]" if not c.known else "trusted",
                tiers[c.domain],
                str(c.item_count),
            )
        console.print(table)

    render()
    if not approve_all:
        console.print(
            "Only approved sites reach the topic editor, and approving one adds it to "
            "config/sources.yaml for every future run.\n"
            "Type numbers to toggle (e.g. [bold]2 5[/]), [bold]a[/] for all, [bold]n[/] for "
            "none, [bold]?N[/] to see what was found on site N, or press Enter to accept."
        )
        while True:
            answer = typer.prompt("Toggle", default="", show_default=False).strip().lower()
            if not answer:
                break
            if answer == "a" or answer == "n":
                chosen = dict.fromkeys(chosen, answer == "a")
            elif answer.startswith("?") and answer[1:].strip().isdigit():
                _show_items(candidates, int(answer[1:].strip()))
                continue
            else:
                for token in answer.replace(",", " ").split():
                    if token.isdigit() and 1 <= int(token) <= len(candidates):
                        domain = candidates[int(token) - 1].domain
                        chosen[domain] = not chosen[domain]
            render()

        # A newly trusted site needs a tier: it decides whether a draft resting
        # on it will pass the Source Checker at all.
        tier_ids = list(get_settings().trust_tiers)
        for candidate in candidates:
            if not chosen[candidate.domain] or candidate.known:
                continue
            tiers[candidate.domain] = typer.prompt(
                f"Trust tier for {candidate.domain} ({'/'.join(tier_ids)})",
                default=candidate.suggested_tier,
            ).strip()

    return [
        SourceDecision(
            domain=c.domain, approved=chosen[c.domain], tier=tiers[c.domain]
        )
        for c in candidates
    ]


def _show_items(candidates, index: int) -> None:
    if not 1 <= index <= len(candidates):
        return
    candidate = candidates[index - 1]
    console.print(f"\n[bold]{candidate.domain}[/] — {candidate.item_count} item(s)")
    for item in candidate.items:
        console.print(f"  • {item.title}\n    [dim]{item.url}[/]\n    {item.why_it_matters}")
    console.print()


# ---------------------------------------------------------------------------


@app.command()
def write(
    index: int = typer.Option(1, "--index", "-n", help="Which suggestion to write (1-based)."),
    topic_file: Path | None = typer.Option(
        None, "--topic", "-t", help="suggestions-*.json file. Defaults to the most recent."
    ),
    dossier: Path | None = typer.Option(
        None,
        "--dossier",
        "-d",
        help="Reuse a saved dossier from research/ instead of researching again.",
    ),
    skip_source_check: bool = typer.Option(
        False,
        "--skip-source-check",
        help="With --dossier, go straight to the writer. Claims will be unverified.",
    ),
    notes: Path | None = typer.Option(
        None,
        "--notes",
        help="Author notes markdown. Defaults to input/notes/<slug>.md. Empty = analysis mode.",
    ),
    push: bool | None = typer.Option(
        None, "--push/--no-push", help="Override WP_AUTO_PUSH for this run."
    ),
    cover: bool | None = typer.Option(
        None, "--cover/--no-cover", help="Override COVER_ENABLED for this run."
    ),
    translate: bool | None = typer.Option(
        None, "--translate/--no-translate", help="Override TRANSLATE_ENABLED for this run."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Use the offline stub client."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show the Agent Framework trace as well."
    ),
) -> None:
    """Research, source-check, write, validate and publish one post draft."""
    setup_logging()
    if verbose:
        import logging

        logging.getLogger("agent_framework").setLevel(logging.INFO)
    from .workflows import write_post

    path = topic_file or storage.latest_topic_file()
    if path is None:
        raise typer.BadParameter("No suggestions file found. Run `ppn suggest` first.")
    suggestions = storage.load_topic_suggestions(Path(path))
    if not 1 <= index <= len(suggestions.suggestions):
        raise typer.BadParameter(f"--index must be between 1 and {len(suggestions.suggestions)}")
    topic = suggestions.suggestions[index - 1]

    console.print(Panel(f"[bold]{topic.title}[/]\n\n{topic.angle}", title="Writing"))

    clients = _clients(dry_run)
    notes_text = _load_notes(notes, topic.slug or topic.title)

    if dossier is not None:
        from .models import ResearchDossier
        from .workflows import write_post_from_dossier

        path = dossier if dossier.is_absolute() else get_settings().run.research_dir / dossier.name
        if not path.exists():
            raise typer.BadParameter(f"No dossier at {path}")
        loaded = ResearchDossier.model_validate_json(path.read_text(encoding="utf-8"))
        console.print(
            f"[green]Reusing research[/] — {len(loaded.claims)} claims, "
            f"{len(loaded.citations)} citations from {path.name}"
            + ("  [yellow](source check skipped)[/]" if skip_source_check else "")
        )
        package = _run_with_progress(
            lambda on_event: write_post_from_dossier(
                topic,
                loaded,
                clients=clients,
                push_to_wordpress=push,
                make_cover=False if dry_run else cover,
                translate=translate,
                skip_source_check=skip_source_check,
                notes_text=notes_text,
                on_event=on_event,
            ),
            timeout_minutes=get_settings().run.write_timeout_minutes,
            what="write",
        )
        _report(package)
        return

    package = _run_with_progress(
        lambda on_event: write_post(
            topic,
            clients=clients,
            push_to_wordpress=push,
            make_cover=False if dry_run else cover,
            translate=translate,
            notes_text=notes_text,
            on_event=on_event,
        ),
        timeout_minutes=get_settings().run.write_timeout_minutes,
        what="write",
    )
    _report(package)


@app.command("write-topic")
def write_topic(
    title: str = typer.Option(..., "--title", help="Working title."),
    angle: str = typer.Option("", "--angle", help="The specific take."),
    problem: str = typer.Option("", "--problem", help="Reader problem the post solves."),
    keyword: str = typer.Option("", "--keyword", help="Primary keyword."),
    area: str = typer.Option("power-apps", "--area", help="Watch area id from topics.yaml."),
    fmt: str = typer.Option("how-to", "--format", help="Post format id from blog_profile.yaml."),
    source: list[str] = typer.Option([], "--source", help="Seed URL. Repeatable."),
    question: list[str] = typer.Option([], "--question", help="Question the research must answer."),
    notes: Path | None = typer.Option(
        None, "--notes", help="Author notes markdown. Defaults to input/notes/<slug>.md."
    ),
    push: bool | None = typer.Option(None, "--push/--no-push"),
    translate: bool | None = typer.Option(
        None, "--translate/--no-translate", help="Also produce the Spanish version."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Write a post for a topic you specify by hand, skipping topic discovery."""
    setup_logging()
    from .util import slugify
    from .workflows import write_post

    topic = TopicSuggestion(
        title=title,
        slug=slugify(title),
        watch_area=area,
        post_format=fmt,
        primary_keyword=keyword or title,
        angle=angle or "Practical, hands-on treatment with real limits called out.",
        problem_statement=problem or f"Readers need a reliable way to handle: {title}",
        why_now="Requested directly by the author.",
        key_questions=list(question),
        seed_sources=list(source),
        novelty="Author-selected angle.",
        audience_fit=4,
        timeliness=3,
        effort=3,
        score=80.0,
    )
    notes_text = _load_notes(notes, topic.slug or topic.title)
    package = asyncio.run(
        write_post(
            topic,
            clients=_clients(dry_run),
            push_to_wordpress=push,
            make_cover=False if dry_run else None,
            translate=translate,
            notes_text=notes_text,
        )
    )
    _report(package)


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run"),
    push: bool | None = typer.Option(None, "--push/--no-push"),
    translate: bool | None = typer.Option(
        None, "--translate/--no-translate", help="Also produce the Spanish version."
    ),
) -> None:
    """Discover topics, then immediately write the top-ranked one."""
    setup_logging()
    from .workflows import discover_topics, write_post

    clients = _clients(dry_run)
    settings = get_settings()
    suggestions = _run_with_progress(
        lambda on_event: discover_topics(clients=clients, on_event=on_event),
        timeout_minutes=settings.run.suggest_timeout_minutes,
        what="suggest",
    )
    if not suggestions.suggestions:
        console.print("[red]No topics suggested.[/]")
        raise typer.Exit(1)
    top = suggestions.suggestions[0]
    console.print(Panel(f"[bold]{top.title}[/]\n\n{top.angle}", title="Top topic"))
    package = _run_with_progress(
        lambda on_event: write_post(
            top,
            clients=clients,
            push_to_wordpress=push,
            make_cover=False if dry_run else None,
            translate=translate,
            on_event=on_event,
        ),
        timeout_minutes=settings.run.write_timeout_minutes,
        what="write",
    )
    _report(package)


# ---------------------------------------------------------------------------
# WordPress
# ---------------------------------------------------------------------------


@wp_app.command("check")
def wp_check() -> None:
    """Verify the WordPress REST connection and Application Password."""
    setup_logging()
    from .wordpress import WordPressClient

    me = asyncio.run(WordPressClient().verify_connection())
    console.print(f"[green]Connected[/] as {me.get('name')} (id {me.get('id')}), "
                  f"roles: {', '.join(me.get('roles', []))}")


@wp_app.command("preview")
def wp_preview(path: Path) -> None:
    """Print the Gutenberg block markup a local draft would produce."""
    from .storage import split_front_matter
    from .util import strip_h1
    from .wordpress import markdown_to_blocks

    _, body = split_front_matter(path.read_text(encoding="utf-8"))
    _, body = strip_h1(body)
    console.print(markdown_to_blocks(body))


@wp_app.command("push")
def wp_push(
    path: Path,
    status: str = typer.Option("draft", "--status", help="draft | pending | publish"),
) -> None:
    """Push an existing local markdown draft to WordPress."""
    setup_logging()
    from .storage import split_front_matter
    from .util import slugify, strip_h1, word_count
    from .wordpress import push_draft

    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    title, _ = strip_h1(body)
    draft = Draft(
        title=front.get("title") or title or path.stem,
        slug=front.get("slug") or slugify(front.get("title") or title or path.stem),
        meta_description=front.get("description", ""),
        primary_keyword=front.get("primary_keyword", ""),
        category=front.get("category", ""),
        tags=front.get("tags", []) or [],
        post_format=front.get("post_format", "how-to"),
        excerpt=front.get("description", ""),
        markdown=body,
        word_count=front.get("word_count") or word_count(body),
    )
    target = asyncio.run(push_draft(draft, status=status))
    console.print(f"[green]{target.status}[/] → {target.edit_link}")


# ---------------------------------------------------------------------------


def _report(package) -> None:
    outcome = package.outcome
    verdict = "[green]APPROVED[/]" if outcome.approved else "[yellow]NEEDS WORK[/]"
    lines = [
        f"{verdict}  score {outcome.overall_score:.1f}  revisions {outcome.revision}  "
        f"blockers {outcome.blockers}",
        f"Voice   {package.voice_mode}"
        + (f"  ·  {len(package.author_claims)} author claims" if package.author_claims else ""),
        "",
        f"Draft   {package.markdown_path}",
        f"Review  {package.report_path}",
    ]
    if package.cover and package.cover.ok:
        lines.append(f"Cover      {package.cover.path}")
    elif package.cover and package.cover.error:
        lines.append(f"Cover      [yellow]failed[/] — {package.cover.error}")
    if package.translation:
        lines.append(
            f"Translated {package.translation_language or 'es'}  {package.translation_path}"
        )
        if package.translation_published:
            lines.append(
                f"           WordPress #{package.translation_published.post_id} "
                f"({package.translation_published.status})"
            )
    if package.published:
        lines.append(f"WordPress  {package.published.status} #{package.published.post_id}")
        if package.published.featured_media_id:
            lines.append(f"Featured   media #{package.published.featured_media_id}")
        lines.append(f"Edit       {package.published.edit_link}")
    else:
        lines.append("WordPress  not pushed")
    console.print(Panel("\n".join(lines), title=package.draft.title))

    if outcome.approved and package.translation is None and package.markdown_path:
        console.print(
            f"[dim]Spanish version not generated. Run "
            f"[bold]ppn translate {package.markdown_path}[/bold] if you want it.[/]"
        )

    if not outcome.approved and outcome.revision_instructions:
        console.print(Panel(outcome.revision_instructions, title="Outstanding findings"))


@app.command()
def cover(
    path: Path = typer.Argument(..., help="A draft markdown file in drafts/."),
    concept: str = typer.Option(
        "", "--concept", "-c", help="Override the visual concept. Blank uses the draft's own."
    ),
    push: bool = typer.Option(
        False, "--push", help="Upload to WordPress and set it as the featured image."
    ),
) -> None:
    """Generate (or regenerate) the cover image for an existing draft.

    Useful when you like the post but not the art — rerun this until the cover
    is right, without paying for the whole pipeline again.
    """
    setup_logging()
    from .covers import build_cover
    from .storage import split_front_matter
    from .util import slugify, strip_h1, word_count

    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    title, _ = strip_h1(body)
    settings = get_settings()
    words = front.get("word_count") or word_count(body)
    wpm = int(settings.structure.get("reading_speed_wpm", 200)) or 200

    draft = Draft(
        title=front.get("title") or title or path.stem,
        slug=front.get("slug") or slugify(front.get("title") or title or path.stem),
        meta_description=front.get("description", ""),
        primary_keyword=front.get("primary_keyword", ""),
        category=front.get("category", ""),
        tags=front.get("tags", []) or [],
        post_format=front.get("post_format", "analisis"),
        markdown=body,
        cover_concept=concept or front.get("cover_concept", ""),
        word_count=words,
        read_minutes=front.get("read_minutes") or max(1, round(words / wpm)),
    )

    result = asyncio.run(build_cover(draft, settings))
    if not result.ok:
        console.print(f"[red]Cover generation failed:[/] {result.error}")
        raise typer.Exit(1)
    console.print(f"[green]Cover written[/] → {result.path}  ({result.width}x{result.height})")
    console.print(f"[dim]{result.prompt[:400]}[/]")

    if push:
        from .wordpress import WordPressClient

        media_id = asyncio.run(
            WordPressClient().upload_media(
                Path(result.path), alt_text=result.alt_text, title=draft.title
            )
        )
        if media_id:
            console.print(f"[green]Uploaded[/] → media #{media_id}. "
                          "Re-run `ppn wp push` on the draft to attach it as featured image.")


@app.command()
def models() -> None:
    """List the model deployments visible on your Foundry resource.

    Use this when cover generation reports DeploymentNotFound — it shows what you
    actually have rather than what the docs say exists.
    """
    setup_logging()
    from .covers import list_image_deployments

    settings = get_settings()
    console.print(f"Provider: [bold]{settings.cover.status_detail}[/]\n")
    try:
        names = asyncio.run(list_image_deployments(settings))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not list deployments:[/] {exc}")
        raise typer.Exit(1) from None

    table = Table(title="Deployments")
    table.add_column("Name")
    table.add_column("Looks like an image model", width=24)
    for name in names:
        lowered = name.lower()
        is_image = any(k in lowered for k in ("image", "dall", "flux", "stable", "sd3"))
        marker = "[green]yes[/]" if is_image else "[dim]no[/]"
        table.add_row(name, marker)
    console.print(table)
    console.print(
        f"\nCOVER_MODEL is currently [bold]{settings.cover.model}[/] "
        f"(route: [bold]{settings.cover.route}[/])."
    )
    if settings.cover.uses_mai:
        from .covers import fit_to_mai_limits

        width, height = fit_to_mai_limits(*settings.cover.dimensions)
        console.print(
            f"[dim]MAI caps images at 1,048,576 pixels, so COVER_SIZE="
            f"{settings.cover.size} will be sent as {width}x{height}. "
            "quality and n are not supported on this route.[/]"
        )
    elif settings.cover.uses_openai:
        console.print(
            "[dim]On OpenAI the GPT Image models need Organization Verification "
            "(platform.openai.com → Settings → Organization → General). API billing is "
            "separate from any ChatGPT subscription.[/]"
        )
    else:
        console.print(
            "[dim]gpt-image-2 is GA. gpt-image-1 / 1.5 / mini are limited access and need "
            "registration before they appear here. dall-e-3 was retired in March 2026. "
            "FLUX-1.1-pro deploys serverless in every region with no registration. "
            "Set COVER_PROVIDER=openai to use api.openai.com instead.[/]"
        )


@app.command()
def translate(
    path: Path = typer.Argument(..., help="An approved draft markdown file in drafts/."),
    push: bool | None = typer.Option(
        None, "--push/--no-push", help="Push the translation to WordPress."
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Translate an existing English draft, without rerunning the pipeline."""
    setup_logging()
    from .agents import build_translator
    from .storage import save_draft, split_front_matter
    from .util import as_json, parse_model, slugify, strip_h1, word_count

    settings = get_settings()
    profile = settings.translation_profile
    front, body = split_front_matter(path.read_text(encoding="utf-8"))
    title, _ = strip_h1(body)
    words = front.get("word_count") or word_count(body)

    source = Draft(
        title=front.get("title") or title or path.stem,
        slug=front.get("slug") or slugify(front.get("title") or title or path.stem),
        meta_description=front.get("description", ""),
        primary_keyword=front.get("primary_keyword", ""),
        category=front.get("category", ""),
        tags=front.get("tags", []) or [],
        post_format=front.get("post_format", "analysis"),
        markdown=body,
        word_count=words,
        read_minutes=front.get("read_minutes") or max(1, round(words / 200)),
    )

    agent = build_translator(settings, _clients(dry_run))
    prompt = (
        f"Translate this approved post into {profile.get('target_language', 'Spanish')}.\n\n"
        f"<english_draft_metadata>\n{as_json(source.model_dump(exclude={'markdown'}))}\n"
        f"</english_draft_metadata>\n\n<english_markdown>\n{source.markdown}\n"
        f"</english_markdown>\n\nReturn the complete translated Draft JSON."
    )

    async def run_translation():
        return await agent.run(prompt)

    translated = parse_model(asyncio.run(run_translation()), Draft)
    suffix = profile.get("slug_suffix", "-es")
    if not translated.slug.endswith(suffix):
        translated.slug = f"{translated.slug or source.slug}{suffix}"
    translated.category = source.category
    if not translated.word_count:
        translated.word_count = word_count(translated.markdown)

    out = save_draft(
        translated,
        None,
        language=profile.get("target_code", "es"),
        translation_of=source.slug,
    )
    console.print(f"[green]Translated[/] → {out}")

    should_push = settings.translation.push if push is None else push
    if should_push and not dry_run:
        from .wordpress import push_draft

        target = asyncio.run(push_draft(translated))
        console.print(f"[green]{target.status}[/] → {target.edit_link}")


@app.command()
def preflight() -> None:
    """Make one cheap real call per agent option-shape before a long run.

    A model that rejects an option fails with a 400 the moment that agent is
    first invoked — which for the writer is several minutes and a full research
    pass into the run. This costs a few seconds and catches it up front.
    """
    setup_logging()
    from pydantic import BaseModel

    from .clients import reasoning_client
    from .settings import get_settings

    settings = get_settings()

    class Ping(BaseModel):
        ok: bool
        word: str

    shapes = [
        ("structured output", {"response_format": Ping}),
        (
            f"structured output + temperature ({settings.foundry.model})",
            {"response_format": Ping, "temperature": 0.5},
        ),
    ]

    async def probe() -> list[tuple[str, bool, str]]:
        from agent_framework import Agent

        results = []
        for label, options in shapes:
            agent = Agent(reasoning_client(), "Reply with ok=true and word='pong'.", name="preflight")
            try:
                await agent.run("ping", options=options)
                results.append((label, True, "accepted"))
            except Exception as exc:  # noqa: BLE001 - reporting is the point
                message = str(exc)
                if "temperature" in message and "not supported" in message:
                    message = "model rejects 'temperature'"
                results.append((label, False, message[:160]))
        return results

    try:
        results = asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not reach the model:[/] {exc}")
        raise typer.Exit(1) from None

    table = Table(title="Preflight")
    table.add_column("Request shape", overflow="fold")
    table.add_column("Result", width=10)
    table.add_column("Detail", overflow="fold")
    for label, ok, detail in results:
        table.add_row(label, "[green]ok[/]" if ok else "[red]rejected[/]", detail)
    console.print(table)

    temp_ok = results[1][1]
    detected = settings.foundry.supports_temperature
    if temp_ok != detected:
        console.print(
            f"[yellow]Mismatch:[/] this build assumes supports_temperature={detected} for "
            f"[bold]{settings.foundry.model}[/], but the service says {temp_ok}. "
            f"Set [bold]FOUNDRY_TEMPERATURE_SUPPORT={'true' if temp_ok else 'false'}[/] in .env."
        )
    elif not temp_ok:
        console.print(
            f"[dim]{settings.foundry.model} is a reasoning model: temperature is omitted "
            "automatically. Nothing to do.[/]"
        )
    else:
        console.print("[green]All request shapes accepted.[/]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
) -> None:
    """Start the management UI and API.

    On first start the YAML files under config/ are imported into the database,
    which then becomes authoritative — edits made in the UI are versioned there
    rather than in git.
    """
    setup_logging()
    try:
        from .server.app import _ui_present
        from .server.app import serve as run_server
    except ImportError as exc:
        console.print(
            f"[red]Server extras are not installed[/] ({exc}).\n"
            "Run: [bold]pip install -e \".[server]\"[/]"
        )
        raise typer.Exit(1) from None

    console.print(f"[bold]API[/]  http://{host}:{port}/api/health")
    if _ui_present():
        console.print(f"[bold]UI[/]   http://{host}:{port}/")
    else:
        console.print(
            "[dim]No built UI found at ui/dist. Run the Vite dev server separately, "
            "or build it once with `npm --prefix ui run build`.[/]"
        )
    run_server(host=host, port=port, reload=reload)


@app.command()
def rules() -> None:
    """Print the rules the validators enforce, as loaded from config."""
    settings = get_settings()
    table = Table(title="Validation rules")
    table.add_column("ID", width=5)
    table.add_column("Group", width=10)
    table.add_column("Severity", width=9)
    table.add_column("Rule", overflow="fold")
    for rule in settings.all_rules():
        colour = {"blocker": "red", "major": "yellow", "minor": "dim", "info": "dim"}.get(
            rule["severity"], "white"
        )
        table.add_row(rule["id"], rule["group"], f"[{colour}]{rule['severity']}[/]", rule["rule"].strip())
    console.print(table)


@config_app.command("reload")
def config_reload() -> None:
    """Re-import config/ into the server database as new versions.

    The server imports config/ once, on first start, and the database is
    authoritative from then on — so a git-only swap of the YAML never reaches a
    running server. This appends the current files as a new version of each
    document (keeping the edit history), which is how a new editorial ruleset
    goes live for the UI and any queued run.
    """
    setup_logging()

    async def run() -> tuple[list[tuple[str, int]], bool]:
        from .server import config_store
        from .server.db import init_db

        await init_db()
        if await config_store.seed_from_yaml_if_empty():
            return [(n, 1) for n in config_store.DOCUMENTS], True
        return await config_store.reimport_from_yaml(note="Reloaded via `ppn config reload`"), False

    try:
        results, seeded = asyncio.run(run())
    except ImportError as exc:
        console.print(
            f"[red]Server extras are not installed[/] ({exc}).\n"
            "The database config store needs: [bold]pip install -e \".[server]\"[/]. "
            "Without the server, the CLI reads config/ directly and no reload is needed."
        )
        raise typer.Exit(1) from None

    verb = "seeded (first import)" if seeded else "reloaded"
    table = Table(title=f"Config {verb}")
    table.add_column("Document")
    table.add_column("New version")
    for name, version in results:
        table.add_row(name, f"v{version}")
    console.print(table)


@app.command("show-config")
def show_config() -> None:
    """Dump the effective configuration (secrets redacted)."""
    settings = get_settings()
    data = {
        "foundry": {"endpoint": settings.foundry.project_endpoint, "model": settings.foundry.model,
                    "fast_model": settings.foundry.fast_model},
        "wordpress": {"api_base": settings.wordpress.api_base, "user": settings.wordpress.username,
                      "status": settings.wordpress.default_status,
                      "auto_push": settings.wordpress.auto_push,
                      "app_password_set": bool(settings.wordpress.app_password)},
        "search": {"provider": settings.search.provider, "key_set": settings.search.is_configured},
        "run": {"max_revision_rounds": settings.run.max_revision_rounds,
                "max_source_rounds": settings.run.max_source_rounds},
        "watch_areas": [a["id"] for a in settings.watch_areas],
        "feeds": len(settings.feeds),
    }
    console.print_json(json.dumps(data))


if __name__ == "__main__":
    app()
