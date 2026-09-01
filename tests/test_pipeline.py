"""End-to-end checks that run entirely offline against the stub client."""

from __future__ import annotations

from pathlib import Path

import pytest

from ppn_blogger.models import PostPackage, SourceReviewSet, TopicSuggestionSet
from ppn_blogger.settings import get_settings
from ppn_blogger.testing import stub_clients
from ppn_blogger.workflows import (
    discover_topics,
    explore_sources,
    shortlist_from_sources,
    write_post,
)


@pytest.mark.asyncio
async def test_topic_discovery_produces_suggestions(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.run, "topics_dir", tmp_path)
    result = await discover_topics(clients=stub_clients())
    assert isinstance(result, TopicSuggestionSet)
    assert result.suggestions
    assert (tmp_path / f"suggestions-{result.generated_on}.json").exists()


@pytest.mark.asyncio
async def test_exploration_stops_at_the_sources_and_resumes_from_them(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.run, "topics_dir", tmp_path)
    clients = stub_clients()

    review = await explore_sources("wide sweep", clients=clients)
    assert isinstance(review, SourceReviewSet)
    assert review.instruction == "wide sweep"
    # The sweep must not have produced topics: nothing may be proposed before a
    # human has said which sites are acceptable.
    assert not list(tmp_path.glob("suggestions-*.json"))
    assert {c.domain for c in review.candidates} == {
        "learn.microsoft.com",
        "matthewdevaney.com",
        "dataverse-notes.example",
    }

    result = await shortlist_from_sources(
        review.reports, ["learn.microsoft.com"], instruction="wide sweep", clients=clients
    )
    assert isinstance(result, TopicSuggestionSet)
    assert result.suggestions
    assert (tmp_path / f"suggestions-{result.generated_on}.json").exists()


@pytest.mark.asyncio
async def test_post_pipeline_exercises_both_loops(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.run, "topics_dir", tmp_path)
    monkeypatch.setattr(settings.run, "output_dir", tmp_path)
    monkeypatch.setattr(settings.run, "research_dir", tmp_path)

    clients = stub_clients(exercise_loops=True)
    topics = await discover_topics(clients=clients)
    package = await write_post(
        topics.suggestions[0], clients=clients, push_to_wordpress=False
    )

    assert isinstance(package, PostPackage)
    # The stub fails the first source check and the first validation round,
    # so a correct graph must have looped exactly once through each.
    assert package.outcome.revision >= 1, "revision loop never fired"
    assert package.outcome.source_verdict is not None
    assert package.outcome.source_verdict.passed, "source loop did not recover"
    assert package.outcome.approved, "draft should be approved on the second review"
    assert package.draft.word_count > 100
    assert package.markdown_path and package.report_path


@pytest.mark.asyncio
async def test_markdown_converts_to_gutenberg_blocks(tmp_path, monkeypatch):
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    from ppn_blogger.wordpress import markdown_to_blocks

    clients = stub_clients(exercise_loops=False)
    topics = await discover_topics(clients=clients)
    package = await write_post(topics.suggestions[0], clients=clients, push_to_wordpress=False)

    blocks = markdown_to_blocks(package.draft.markdown)
    assert "<!-- wp:heading" in blocks
    assert "<!-- wp:code" in blocks
    assert "<!-- wp:table" in blocks
    assert "<!-- wp:list" in blocks
    assert "<!-- wp:quote" in blocks
    # No in-body images any more: the converter has no image path at all.
    assert "wp:image" not in blocks
    assert "ppn-image-placeholder" not in blocks
    # Gutenberg comments must be balanced. Self-closing blocks (`<!-- wp:x /-->`)
    # are complete on their own and have no closing delimiter.
    self_closing = blocks.count("/-->")
    assert blocks.count("<!-- wp:") - self_closing == blocks.count("<!-- /wp:")


def test_code_blocks_serialise_exactly_like_core_code():
    """Gutenberg validates by re-running save() and diffing the markup.

    Every deviation from core/code's own serialisation shows up in the editor as
    "this block contains unexpected or invalid content" — which is what happened
    on the first real post, where a JSON snippet was flagged on every line.
    """
    from ppn_blogger.wordpress import markdown_to_blocks

    body = (
        "```json\n"
        '{"ConnectorId": "shared_sharepointonline", "AllowedActions": ["GetItem"]}\n'
        "```\n"
    )
    blocks = markdown_to_blocks(body)

    # Quotes stay literal. html.escape() turns them into &quot;, and core/code
    # does not — that single difference invalidated the whole block.
    assert '{"ConnectorId": "shared_sharepointonline"' in blocks
    assert "&quot;" not in blocks
    # Brackets are escaped so a snippet can never be parsed as a shortcode.
    assert "&#91;" in blocks and '["GetItem"]' not in blocks
    # core/code emits a bare <code> — a class attribute is not part of save().
    assert "<code>" in blocks
    assert 'class="language-' not in blocks
    assert '<pre class="wp-block-code">' in blocks
    # The language survives in the block attributes, which core ignores and the
    # syntax-highlighting plugin reads.
    assert '<!-- wp:code {"language":"json"} -->' in blocks


def test_html_entities_in_code_are_escaped_once():
    from ppn_blogger.wordpress import markdown_to_blocks

    blocks = markdown_to_blocks("```xml\n<Group name=\"A & B\"/>\n```\n")
    assert "&lt;Group name=\"A &amp; B\"/&gt;" in blocks
    assert "&amp;amp;" not in blocks, "double-escaped ampersand"


def test_no_image_path_in_the_converter():
    """The in-body image path is gone: markdown images never become blocks.

    S11 blocks images upstream, but if one slips through it must not resurrect
    the old empty-core/image slot the converter used to emit.
    """
    from ppn_blogger.wordpress import markdown_to_blocks

    blocks = markdown_to_blocks("## Step\n\n![alt](IMAGE:foo)\n\nText.\n")
    assert "ppn-screenshot-slot" not in blocks
    assert 'wp:image {"className"' not in blocks


@pytest.mark.asyncio
async def test_a_cover_uploads_once_per_image(tmp_path, monkeypatch):
    """Publishing twice must not fill the media library with the same PNG.

    Every publish now carries the cover, and publish is a button pressed more
    than once on the same post. The memo is keyed by the image's own bytes, so a
    regenerated cover does upload — that is the case the whole feature exists for.
    """
    from ppn_blogger import wordpress
    from ppn_blogger.models import CoverImage
    from ppn_blogger.settings import get_settings

    settings = get_settings()
    for attr, value in (("url", "https://blog.test"), ("username", "u"), ("app_password", "p")):
        monkeypatch.setattr(settings.wordpress, attr, value)
    monkeypatch.setattr(wordpress, "MEDIA_STATE_FILE", tmp_path / "wp_media.json")

    uploads = []

    async def fake_upload(self, path, *, alt_text="", title="", strict=False):
        uploads.append(path.read_bytes())
        return 100 + len(uploads)

    monkeypatch.setattr(wordpress.WordPressClient, "upload_media", fake_upload)

    art = tmp_path / "post.png"
    art.write_bytes(b"first-image")
    client = wordpress.WordPressClient()

    assert await client.ensure_media("post", CoverImage(path=str(art))) == 101
    assert await client.ensure_media("post", CoverImage(path=str(art))) == 101
    assert len(uploads) == 1, "the same image was uploaded twice"

    art.write_bytes(b"regenerated-image")
    assert await client.ensure_media("post", CoverImage(path=str(art))) == 102
    assert len(uploads) == 2, "new art did not reach WordPress"


def test_rules_load_and_are_non_empty():
    settings = get_settings()
    rules = settings.all_rules()
    assert len(rules) >= 20
    # v2 ruleset plus the focus family: seven families instead of three.
    assert {r["group"] for r in rules} == {
        "honesty", "typography", "voice", "content", "focus", "structure", "seo"
    }
    assert all(r["severity"] in {"blocker", "major", "minor", "info"} for r in rules)


@pytest.mark.asyncio
async def test_streaming_path_matches_non_streaming(tmp_path, monkeypatch):
    """The CLI streams so it can show progress — the stub must stream too.

    Guards against --dry-run silently exercising a different code path than a
    real run, which is how a dry run stops being worth anything.
    """
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients = stub_clients(exercise_loops=True)
    events: list[object] = []

    topics = await discover_topics(clients=clients, on_event=events.append)
    assert topics.suggestions
    assert events, "streaming produced no events"

    package = await write_post(
        topics.suggestions[0], clients=clients, push_to_wordpress=False, on_event=events.append
    )
    assert isinstance(package, PostPackage)
    # Streaming must not skip the loops the non-streaming test relies on.
    assert package.outcome.revision >= 1
    assert package.outcome.source_verdict is not None
    assert package.outcome.source_verdict.passed


def test_search_provider_switches_the_web_search_tool(monkeypatch):
    """foundry -> hosted tool only; tavily -> local web_search function."""
    from ppn_blogger.agents import _searchable
    from ppn_blogger.tools import RESEARCHER_TOOLS, web_search

    settings = get_settings()
    stub = stub_clients().reasoning  # stub cannot provide a hosted tool

    monkeypatch.setattr(settings.search, "provider", "foundry")
    assert settings.search.is_configured, "foundry needs no API key"
    assert web_search not in _searchable(RESEARCHER_TOOLS, stub)

    monkeypatch.setattr(settings.search, "provider", "tavily")
    assert web_search in _searchable(RESEARCHER_TOOLS, stub)

    monkeypatch.setattr(settings.search, "provider", "none")
    resolved = _searchable(RESEARCHER_TOOLS, stub)
    assert web_search not in resolved
    # Feeds and Learn survive, so the crew still works without open-web search.
    assert any(getattr(t, "name", "") == "search_microsoft_learn" for t in resolved)


def test_house_structure_rules_are_enforceable_on_the_sample():
    """The sample draft is the spec for the house shape — keep it honest.

    Mirrors the checks the design validator makes, so a drift in the sample or
    in the structure config shows up here rather than in a published post.
    """
    import re

    from ppn_blogger.testing import _SAMPLE_MARKDOWN

    settings = get_settings()
    structure = settings.structure
    md = _SAMPLE_MARKDOWN

    assert len(re.findall(r"^# ", md, re.M)) == 1, "exactly one H1"
    assert not re.search(r"^### ", md, re.M), "H3 is banned by S01"

    headings = re.findall(r"^## (.+)$", md, re.M)
    toc_heading = structure["toc_heading"]
    sources_heading = structure["sources_heading"]
    assert headings[0] == toc_heading, "the table of contents comes first"
    assert headings[-1] == sources_heading, "Fuentes is last"

    body = [h for h in headings if h not in {toc_heading, sources_heading}]
    assert structure["min_sections"] <= len(body) <= structure["max_sections"]
    assert body[-2] == structure["critical_section_heading"], "critical section is penultimate"
    assert body[-1] in structure["closing_headings"], "closing section title"

    # Table of contents entries must match the body headings, in order.
    toc_block = md.split(f"## {toc_heading}", 1)[1].split("\n## ", 1)[0]
    toc_entries = re.findall(r"^- \[([^\]]+)\]\(#", toc_block, re.M)
    assert toc_entries == body, f"ToC drifted from headings: {toc_entries} != {body}"

    # Every fenced code block declares a language (S08).
    for fence in re.findall(r"^```(.*)$", md, re.M)[::2]:
        assert fence.strip(), "code fence without a language tag"

    # No inline citations in the body (S07) — links live only under Fuentes.
    body_text = md.split(f"## {sources_heading}", 1)[0]
    assert not re.search(r"\]\(https?://", body_text), "inline citation found in the body"

    assert structure["critical_section_heading"] in md
    assert md.count("> **Importante:**") <= structure["max_callouts"]


def test_cover_prompt_is_neon_graphic_and_text_free():
    """The cover is pure artwork now — the prompt must forbid any typography."""
    from ppn_blogger.covers import build_prompt
    from ppn_blogger.testing import _draft

    settings = get_settings()
    draft = _draft(revision=1)
    prompt = build_prompt(draft, settings)
    lowered = prompt.lower()

    assert "neon" in lowered, "art direction lost its neon aesthetic"
    # The post's own subject must drive the image, not a generic scene.
    assert draft.cover_concept[:40] in prompt
    assert draft.category in prompt
    # Every way a model might be told to draw words has to be excluded.
    for banned in ("no text", "no letters", "no logos", "no watermarks", "no typography"):
        assert banned in lowered, f"prompt does not exclude {banned!r}"

    # Category-specific palette, so covers differ by topic area.
    other = _draft(revision=1)
    other.category = "Copilot Studio"
    assert build_prompt(other, settings) != prompt


@pytest.mark.asyncio
async def test_translation_is_opt_in(tmp_path, monkeypatch):
    """Default is English only — translation happens per draft, on request."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)
    assert not settings.translation.enabled, "translation must default to off"

    clients = stub_clients(exercise_loops=False)
    topics = await discover_topics(clients=clients)
    package = await write_post(
        topics.suggestions[0], clients=clients, push_to_wordpress=False, make_cover=False
    )
    assert package.outcome.approved
    assert package.translation is None, "translation ran without being asked for"


@pytest.mark.asyncio
async def test_translation_runs_when_requested(tmp_path, monkeypatch):
    """`--translate` on an approved draft produces the localised sibling."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients = stub_clients(exercise_loops=False)
    topics = await discover_topics(clients=clients)
    package = await write_post(
        topics.suggestions[0],
        clients=clients,
        push_to_wordpress=False,
        make_cover=False,
        translate=True,
    )

    assert package.outcome.approved
    assert package.translation is not None, "translation stage never ran"
    assert package.translation_language == "es"
    assert package.translation.slug.endswith("-es"), "language suffix not applied"
    # Taxonomy stays in the English vocabulary so WordPress categories do not fork.
    assert package.translation.category == package.draft.category
    assert package.translation.markdown != package.draft.markdown
    assert Path(package.translation_path).exists()
    # Localised structure headings, and code left untouched.
    assert "## Contenido" in package.translation.markdown
    assert "## Fuentes" in package.translation.markdown
    assert "pac data export" in package.translation.markdown


@pytest.mark.asyncio
async def test_translation_skipped_when_not_approved(tmp_path, monkeypatch):
    """Never localise a draft the validators rejected."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)
    # One revision round is not enough for the stub to reach a passing score.
    monkeypatch.setattr(settings.run, "max_revision_rounds", 0)

    clients = stub_clients(exercise_loops=True)
    topics = await discover_topics(clients=clients)
    package = await write_post(
        topics.suggestions[0],
        clients=clients,
        push_to_wordpress=False,
        make_cover=False,
        translate=True,      # asked for, but must still be refused
    )

    assert not package.outcome.approved
    assert package.translation is None, "rejected draft must not be translated"


def test_trust_classification():
    from ppn_blogger.tools import classify_domain

    assert classify_domain("https://learn.microsoft.com/en-us/power-apps/x")[0] == "official"
    assert classify_domain("https://www.reddit.com/r/PowerApps/x")[0] == "community_unverified"
    assert classify_domain("https://totally-unknown-blog.example/x")[0] == "unknown"


def test_mai_size_fitting_respects_the_pixel_cap():
    """MAI rejects >1,048,576 px; fit instead of letting the service 400."""
    from ppn_blogger.covers import MAI_MAX_PIXELS, MAI_MIN_EDGE, fit_to_mai_limits

    # The old default was over the cap — this is the case that would have failed.
    assert fit_to_mai_limits(1536, 1024) == (1248, 832)
    assert 1536 * 1024 > MAI_MAX_PIXELS

    for width, height in [(1536, 1024), (1024, 1024), (2048, 512), (3000, 1000), (800, 800)]:
        w, h = fit_to_mai_limits(width, height)
        assert w * h <= MAI_MAX_PIXELS, f"{width}x{height} fitted to {w}x{h}, still over cap"
        assert min(w, h) >= MAI_MIN_EDGE, f"{w}x{h} breaks the minimum edge"
        assert w % 16 == 0 and h % 16 == 0, f"{w}x{h} not a multiple of 16"

    # Aspect ratio is preserved for the common landscape case.
    w, h = fit_to_mai_limits(1536, 1024)
    assert abs((w / h) - 1.5) < 0.01

    # Something already inside the budget is left alone.
    assert fit_to_mai_limits(1248, 832) == (1248, 832)


def test_mai_models_take_the_mai_route(monkeypatch):
    """MAI does not speak the OpenAI images API — detect it from the model name."""
    settings = get_settings()

    monkeypatch.setattr(settings.cover, "provider", "foundry")
    monkeypatch.setattr(settings.cover, "model", "MAI-Image-2.5-Pro")
    assert settings.cover.uses_mai
    assert settings.cover.route == "mai"

    # An OpenAI-compatible deployment on the same resource must not be rerouted.
    monkeypatch.setattr(settings.cover, "model", "gpt-image-2")
    assert not settings.cover.uses_mai
    assert settings.cover.route == "azure-openai"

    # Forcing the route works even for an unrecognised name.
    monkeypatch.setattr(settings.cover, "provider", "mai")
    monkeypatch.setattr(settings.cover, "model", "my-custom-deployment")
    assert settings.cover.uses_mai

    # COVER_PROVIDER=openai always wins.
    monkeypatch.setattr(settings.cover, "provider", "openai")
    monkeypatch.setattr(settings.cover, "model", "MAI-Image-2.5-Pro")
    assert not settings.cover.uses_mai
    assert settings.cover.route == "openai"


def test_cover_provider_switches_client(monkeypatch):
    """COVER_PROVIDER=openai must hit api.openai.com, not the Azure resource."""
    from ppn_blogger.covers import image_client

    settings = get_settings()

    monkeypatch.setattr(settings.cover, "provider", "openai")
    monkeypatch.setattr(settings.cover, "openai_api_key", "sk-test-not-a-real-key")
    assert settings.cover.is_configured
    client = image_client(settings)
    assert "openai.com" in str(client.base_url)

    # A missing key must fail with an explanation, not a confusing auth error.
    monkeypatch.setattr(settings.cover, "openai_api_key", "")
    assert not settings.cover.is_configured
    with pytest.raises(RuntimeError, match="ChatGPT Plus/Pro subscription does not include"):
        image_client(settings)

    monkeypatch.setattr(settings.cover, "provider", "foundry")
    monkeypatch.setattr(settings.cover, "endpoint", "https://example.services.ai.azure.com")
    monkeypatch.setattr(settings.cover, "api_key", "azure-test-key")
    assert "example.services.ai.azure.com" in str(image_client(settings).base_url)


def test_temperature_is_omitted_for_reasoning_models(monkeypatch):
    """gpt-5 returns 400 for `temperature`; the writer must not send one.

    This is the bug that killed a real run six minutes in, after the research
    had already been paid for.
    """
    from ppn_blogger.agents import _opts, build_writer
    from ppn_blogger.models import Draft

    settings = get_settings()
    clients = stub_clients()

    monkeypatch.setattr(settings.foundry, "model", "gpt-5")
    monkeypatch.setattr(settings.foundry, "temperature_support", "auto")
    assert not settings.foundry.supports_temperature
    assert "temperature" not in _opts(Draft, temperature=0.7)
    assert "temperature" not in (build_writer(settings, clients).default_options or {})

    # A model that does accept it still gets it — determinism matters for the
    # validators and creativity for the writer.
    monkeypatch.setattr(settings.foundry, "model", "gpt-4.1")
    assert settings.foundry.supports_temperature
    assert _opts(Draft, temperature=0.7)["temperature"] == 0.7

    # Explicit override wins over detection, both ways.
    monkeypatch.setattr(settings.foundry, "model", "gpt-5")
    monkeypatch.setattr(settings.foundry, "temperature_support", "true")
    assert settings.foundry.supports_temperature
    monkeypatch.setattr(settings.foundry, "model", "gpt-4.1")
    monkeypatch.setattr(settings.foundry, "temperature_support", "false")
    assert not settings.foundry.supports_temperature


@pytest.mark.asyncio
async def test_resume_from_dossier_skips_research(tmp_path, monkeypatch):
    """A saved dossier must be reusable without paying for research again."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    from ppn_blogger.testing import _dossier
    from ppn_blogger.workflows import write_post_from_dossier

    clients = stub_clients(exercise_loops=False)
    topics = await discover_topics(clients=clients)
    dossier = _dossier(clean=True)

    package = await write_post_from_dossier(
        topics.suggestions[0],
        dossier,
        clients=clients,
        push_to_wordpress=False,
        make_cover=False,
    )
    assert package.outcome.approved
    # The reused dossier is the one that reached the writer, not a fresh one.
    assert package.dossier.topic_title == dossier.topic_title
    assert len(package.dossier.claims) == len(dossier.claims)
    assert Path(package.markdown_path).exists()


def test_resume_graph_excludes_the_researcher():
    """The resume graph must not contain a researcher node to fall back into."""
    from agent_framework import WorkflowViz

    from ppn_blogger.workflows import build_post_workflow

    clients = stub_clients()
    normal = WorkflowViz(build_post_workflow(clients=clients).workflow).to_mermaid()
    resumed = WorkflowViz(
        build_post_workflow(clients=clients, resume_from_dossier=True).workflow
    ).to_mermaid()

    assert "researcher" in normal and "brief_builder (Start)" in normal
    assert "researcher" not in resumed, "resume path must not re-enter research"
    assert "dossier_entry (Start)" in resumed
    assert "source_checker --> source_gate" in resumed


# ---------------------------------------------------------------------------
# v2 editorial ruleset: detectors, voice modes, author notes
# ---------------------------------------------------------------------------


def test_validation_rules_parse_and_all_detectors_compile():
    """The ruleset loads and every one of its 22 detectors compiles."""
    import re

    from ppn_blogger.detectors import compile_all

    settings = get_settings()
    compiled = compile_all(settings)
    assert len(compiled) == 22, f"expected 22 detectors, got {len(compiled)}"
    assert all(isinstance(p, re.Pattern) for p in compiled.values())
    # The seven families are all present.
    assert {r["group"] for r in settings.all_rules()} == {
        "honesty", "typography", "voice", "content", "focus", "structure", "seo"
    }


def test_t01_fires_on_prose_dash_and_is_silent_in_code_and_urls():
    """T01: an em dash in prose fires; a hyphen in a compound, slug, URL or a
    dash inside a fenced code block does not."""
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()

    def t01(md: str) -> bool:
        run = run_detectors(md, groups=("typography",), settings=settings, slug="ok")
        return any(f.rule_id == "T01" for f in run.findings)

    assert t01("## H\nThe cache holds for six hours — then it drops.\n"), "em dash in prose"
    assert not t01("## H\nThis is a low-code approach for makers.\n"), "compound hyphen"
    assert not t01("## H\nThe slug is my-post-about-dataverse here.\n"), "slug hyphen"
    assert not t01("## H\nSee https://learn.microsoft.com/a-b-c-d for more.\n"), "url hyphen"
    assert not t01("## H\nRun it now.\n\n```bash\necho a — b\n```\n"), "dash inside a fence"
    # And T02: the spaced hyphen fires in prose but a list bullet does not.
    def t02(md: str) -> bool:
        run = run_detectors(md, groups=("typography",), settings=settings, slug="ok")
        return any(f.rule_id == "T02" for f in run.findings)

    assert t02("## H\nIt works - it just does not scale.\n"), "spaced hyphen in prose"
    assert not t02("## H\n- first bullet\n- second bullet\n"), "list bullets are exempt"


def test_s11_blocks_any_image():
    """S11 is a blocker on markdown image syntax and on IMAGE: markers."""
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()

    def s11(md: str):
        return [
            f for f in run_detectors(md, groups=("structure",), settings=settings).findings
            if f.rule_id == "S11"
        ]

    md_img = s11("## Step\n![a screenshot](https://x/y.png)\n")
    assert md_img and md_img[0].severity == "blocker", "markdown image must trip S11"
    assert s11("## Step\nSee IMAGE:elastic-config for the screen.\n"), "IMAGE: marker trips S11"
    assert not s11("## Step\nNo pictures here, only prose.\n"), "clean section is silent"


def test_h03_trips_on_a_number_absent_from_dossier_and_notes():
    """A measured number in neither the dossier nor the author claims is H03."""
    from ppn_blogger.detectors import run_detectors
    from ppn_blogger.models import AuthorClaim

    settings = get_settings()
    body = "## Result\nThe query returned 5,000 rows in 3 seconds on the first run.\n"

    absent = run_detectors(body, groups=("honesty",), settings=settings, dossier_blob="nothing relevant")
    assert any(f.rule_id == "H03" and f.severity == "blocker" for f in absent.findings)

    # Traceable to the dossier: silent.
    ok = run_detectors(
        body, groups=("honesty",), settings=settings,
        dossier_blob="the unfiltered query returned 5000 rows in 3 seconds",
    )
    assert not any(f.rule_id == "H03" for f in ok.findings)

    # Traceable to an author claim instead: also silent.
    claim = AuthorClaim(id="A1", type="measurement", text="It returned 5,000 rows in 3 seconds.")
    ok2 = run_detectors(body, groups=("honesty",), settings=settings, author_claims=[claim])
    assert not any(f.rule_id == "H03" for f in ok2.findings)


@pytest.mark.asyncio
async def test_no_notes_yields_analysis_and_no_first_person(tmp_path, monkeypatch):
    """`ppn write --dry-run` with no notes file → analysis mode, zero first person."""
    import re

    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients = stub_clients(exercise_loops=False)
    topics = await discover_topics(clients=clients)
    package = await write_post(
        topics.suggestions[0], clients=clients, push_to_wordpress=False, make_cover=False
    )

    assert package.voice_mode == "analysis"
    assert package.author_claims == []
    # No first-person sentences in the analysis draft. Headings and the ToC are
    # excluded so the fixed "My take" section title is not a false hit.
    prose = "\n".join(
        line
        for line in package.draft.markdown.splitlines()
        if not line.startswith("#") and not re.match(r"\s*- \[", line)
    )
    assert not re.search(r"(?i)\b(I|I'm|I've|I'd|my|we|our|us)\b", prose)


@pytest.mark.asyncio
async def test_notes_yield_field_report_with_traceable_claim_ids(tmp_path, monkeypatch):
    """A populated notes fixture → field_report, with traceable author claim ids."""
    import json

    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    notes = (
        "## What I actually did\nRebuilt the table by hand.\n\n"
        "## Numbers I measured\n- 40,000 rows in 11 seconds, my tenant, 14 July\n\n"
        "## What did not work\nThe designer toggle had no effect until I recreated the table.\n"
    )
    clients = stub_clients(exercise_loops=False)
    topics = await discover_topics(clients=clients)
    package = await write_post(
        topics.suggestions[0], clients=clients, push_to_wordpress=False,
        make_cover=False, notes_text=notes,
    )

    assert package.voice_mode == "field_report"
    assert package.author_claims, "notes produced no claims"
    ids = [c.id for c in package.author_claims]
    assert ids == sorted(ids), "claim ids are not stable/ordered"
    # The claims are filed to disk next to the dossier, and every id is traceable.
    saved = json.loads(Path(package.notes_path).read_text())
    assert {c["id"] for c in saved["claims"]} == set(ids)


def test_editor_instructions_helper_wraps_only_when_present():
    from ppn_blogger.executors import RunState, _editor_instructions

    assert _editor_instructions(RunState()) == ""
    out = _editor_instructions(RunState(extra_instructions="  shorten it  "))
    assert "<editor_instructions>" in out and "shorten it" in out


def _capturing_clients():
    """A stub client bundle that records the writer's first-draft prompts."""
    from ppn_blogger.clients import ClientBundle
    from ppn_blogger.models import Draft
    from ppn_blogger.testing import StubChatClient

    captured: list[str] = []

    class _Capturing(StubChatClient):
        def _payload(self, model, messages):
            if model is Draft:
                full = " ".join(m.text or "" for m in messages)
                if "Write the first draft" in full:
                    captured.append(full)
            return super()._payload(model, messages)

    client = _Capturing(exercise_loops=False)
    return ClientBundle(reasoning=client, fast=client), captured


@pytest.mark.asyncio
async def test_editor_instructions_reach_the_writer_fresh(tmp_path, monkeypatch):
    """The fresh-write path (SourceGate) injects the editor instructions."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients, captured = _capturing_clients()
    topics = await discover_topics(clients=clients)
    await write_post(
        topics.suggestions[0],
        clients=clients,
        push_to_wordpress=False,
        extra_instructions="LEAD WITH THE MIGRATION STEPS",
    )

    assert captured, "writer never received a first-draft prompt"
    assert any(
        "<editor_instructions>" in p and "LEAD WITH THE MIGRATION STEPS" in p for p in captured
    )


@pytest.mark.asyncio
async def test_editor_instructions_reach_the_writer_resume(tmp_path, monkeypatch):
    """The resume-from-dossier path (DossierEntry) injects them too."""
    from ppn_blogger.testing import _dossier
    from ppn_blogger.workflows import write_post_from_dossier

    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients, captured = _capturing_clients()
    topics = await discover_topics(clients=clients)
    await write_post_from_dossier(
        topics.suggestions[0],
        _dossier(clean=True),
        clients=clients,
        push_to_wordpress=False,
        skip_source_check=True,
        extra_instructions="DROP THE FAQ SECTION",
    )

    assert captured, "writer never received a first-draft prompt"
    assert any("<editor_instructions>" in p and "DROP THE FAQ SECTION" in p for p in captured)


# ---------------------------------------------------------------------------
# The outline stage: the plan, the gate that checks it, and the thesis that
# has to survive to the end of the run.
# ---------------------------------------------------------------------------


def test_the_outline_sits_between_the_source_gate_and_the_writer():
    """The writer is never reachable without passing through the outline."""
    from agent_framework import WorkflowViz

    from ppn_blogger.workflows import build_post_workflow

    graph = WorkflowViz(build_post_workflow(clients=stub_clients()).workflow).to_mermaid()

    assert "source_gate --> outliner" in graph
    assert "outliner --> outline_gate" in graph
    assert "outline_gate --> writer" in graph
    assert "source_gate --> writer" not in graph, "the writer must not bypass the outline"


def test_resume_with_skip_source_check_still_outlines():
    """The regeneration path is the one most likely to lose the outline.

    `server/runs.py` takes it on every "regenerate reusing prior research", and a
    stale `target_id=A.WRITER` here would be a silent no-op rather than an error.
    """
    from agent_framework import WorkflowViz

    from ppn_blogger.workflows import build_post_workflow

    graph = WorkflowViz(
        build_post_workflow(
            clients=stub_clients(), resume_from_dossier=True, skip_source_check=True
        ).workflow
    ).to_mermaid()

    assert "dossier_entry --> outliner" in graph
    assert "dossier_entry --> writer" not in graph


def test_the_outline_stage_adds_no_round_counter():
    """CLAUDE.md: source_round and revision_round are the only counters.

    The outline gate repairs deterministically instead of looping, which is what
    lets that invariant keep holding. Encoded as a test so a future "just one
    more retry" has to argue with the build.
    """
    import dataclasses

    from ppn_blogger.executors import RunState

    counters = {f.name for f in dataclasses.fields(RunState) if f.name.endswith("_round")}
    assert counters == {"source_round", "revision_round"}


def _repaired(outline, dossier=None, thesis=""):
    from ppn_blogger.executors import repair_outline
    from ppn_blogger.testing import _dossier

    settings = get_settings()
    return repair_outline(
        outline,
        dossier or _dossier(clean=True),
        settings,
        band=settings.word_target("deep-dive", "analysis"),
        fallback_thesis=thesis,
    )


def test_outline_gate_drops_claim_ids_the_dossier_does_not_have():
    from ppn_blogger.testing import _outline

    repaired = _repaired(_outline())

    assert "C99" not in repaired.selected_claim_ids
    assert all("C99" not in s.claim_ids for s in repaired.sections)
    assert any("C99" in w for w in repaired.warnings)


def test_outline_gate_drops_a_section_with_no_claims_but_keeps_the_closing_one():
    from ppn_blogger.testing import _outline

    repaired = _repaired(_outline())
    titles = [s.title for s in repaired.sections]

    assert "Whatever the dossier had left over" not in titles
    # The closing section carries no claims either, and must survive: it is an
    # opinion, not a statement of fact, so it has nothing to trace to.
    assert titles[-1] == "My take"
    assert repaired.sections[-1].claim_ids == []


def test_outline_gate_synthesises_out_of_scope_from_the_unselected_claims():
    from ppn_blogger.testing import _outline

    outline = _outline()
    assert outline.out_of_scope == [], "the canned outline must arrive with none"

    repaired = _repaired(outline)

    assert repaired.out_of_scope, "an empty out_of_scope must never reach the writer"
    assert any("capacity meter" in s for s in repaired.out_of_scope)
    assert any("excluded nothing" in w for w in repaired.warnings)


def test_outline_gate_repairs_rather_than_rejecting():
    """A maximally broken outline still yields something usable, with warnings."""
    from ppn_blogger.models import OutlineSection, PostOutline

    broken = PostOutline(
        thesis="",
        reader_promise="",
        out_of_scope=[],
        sections=[
            OutlineSection(
                title=f"Section {i}",
                makes_this_point="x",
                claim_ids=["NOPE"],
                target_words=9000,
            )
            for i in range(20)
        ],
    )

    repaired = _repaired(broken, thesis="The angle from the topic.")

    assert repaired.thesis == "The angle from the topic."
    assert len(repaired.sections) <= get_settings().structure["max_sections"]
    ceiling = get_settings().structure["max_section_words"]
    assert all(s.target_words <= ceiling for s in repaired.sections)
    assert len(repaired.warnings) >= 3


def test_too_few_sections_passes_through_with_a_warning():
    """The one irreparable case. Producing a thin draft beats producing none."""
    from ppn_blogger.models import OutlineSection, PostOutline

    thin = PostOutline(
        thesis="One thing.",
        reader_promise="y",
        out_of_scope=["something else"],
        sections=[
            OutlineSection(title="Only section", makes_this_point="x", claim_ids=["C1"]),
            OutlineSection(title="My take", makes_this_point="x", claim_ids=[]),
        ],
    )

    repaired = _repaired(thin)

    assert len(repaired.sections) == 2, "nothing was invented to reach the floor"
    assert any("floor" in w for w in repaired.warnings)


def _prompt_capturing_clients():
    """Records the outliner, writer and content-validator prompts of a run."""
    from ppn_blogger.clients import ClientBundle
    from ppn_blogger.models import Draft, PostOutline, ValidationReportDraft
    from ppn_blogger.testing import StubChatClient

    seen: dict[str, list[str]] = {"outline": [], "draft": [], "revision": [], "validate": []}

    class _Capturing(StubChatClient):
        def _payload(self, model, messages):
            full = " ".join(m.text or "" for m in messages)
            if model is PostOutline:
                seen["outline"].append(full)
            elif model is Draft:
                # `full` is the whole conversation, so by the revision turn it
                # still carries the first-draft brief. Match the later marker
                # first or every revision looks like a first draft.
                if "The validators reviewed revision" in full:
                    seen["revision"].append(full)
                elif "Write the first draft" in full:
                    seen["draft"].append(full)
            elif model is ValidationReportDraft:
                seen["validate"].append(full)
            return super()._payload(model, messages)

    client = _Capturing(exercise_loops=True)
    return ClientBundle(reasoning=client, fast=client), seen


@pytest.mark.asyncio
async def test_the_writer_sees_only_the_claims_the_outline_selected(tmp_path, monkeypatch):
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients, seen = _prompt_capturing_clients()
    topics = await discover_topics(clients=clients)
    await write_post(topics.suggestions[0], clients=clients, push_to_wordpress=False)

    assert seen["draft"], "the writer never received a first draft brief"
    brief = seen["draft"][0]
    research = brief.split("<research>", 1)[1].split("</research>", 1)[0]

    # C3 is the claim the canned outline deliberately leaves unused. It must be
    # absent from the research the writer may draw on, and named in the block
    # that tells it what was cut. Silently withholding research invites the
    # writer to fill the gap from memory, which is the one thing it must not do.
    assert "C3" not in research and "capacity meter" not in research
    assert "<omitted_research>" in brief
    assert "C3" in brief.split("<omitted_research>", 1)[1]
    assert "rollup columns" in research, "the selected claims must still be there"
    # Superseded by the real outline; shipping both invites the wrong plan.
    assert "suggested_outline" not in research
    assert "<approved_outline>" in brief and "<thesis>" in brief


@pytest.mark.asyncio
async def test_the_revision_prompt_carries_the_thesis_and_the_draft(tmp_path, monkeypatch):
    """Three rewrites used to happen with no statement of what the post argues."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients, seen = _prompt_capturing_clients()
    topics = await discover_topics(clients=clients)
    await write_post(topics.suggestions[0], clients=clients, push_to_wordpress=False)

    assert seen["revision"], "the revision loop never ran"
    revision = seen["revision"][0]
    assert "<thesis>" in revision
    assert "<approved_outline>" in revision
    # Self-contained: the draft is resent rather than relied on from the session.
    assert "<current_draft_markdown>" in revision
    assert "## What to watch carefully" in revision


@pytest.mark.asyncio
async def test_only_the_content_validator_gets_the_outline(tmp_path, monkeypatch):
    """The split is load-bearing: design judges shape, content judges argument."""
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    clients, seen = _prompt_capturing_clients()
    topics = await discover_topics(clients=clients)
    await write_post(topics.suggestions[0], clients=clients, push_to_wordpress=False)

    content = [p for p in seen["validate"] if "you own the content families" in p.lower()]
    design = [p for p in seen["validate"] if "you own the design families" in p.lower()]
    assert content and design
    assert all("<approved_outline>" in p for p in content)
    assert all("<approved_outline>" not in p for p in design)


@pytest.mark.asyncio
async def test_the_thesis_survives_into_the_package_and_the_front_matter(tmp_path, monkeypatch):
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    topics = await discover_topics(clients=stub_clients())
    package = await write_post(
        topics.suggestions[0], clients=stub_clients(), push_to_wordpress=False
    )

    assert package.outline is not None and package.outline.thesis
    assert package.draft.thesis == package.outline.thesis
    assert package.outline_path and Path(package.outline_path).exists()
    assert "thesis:" in Path(package.markdown_path).read_text(encoding="utf-8")
    # The review report answers "did this stay on its argument" without opening
    # the draft, which is the whole point of writing it there.
    report = Path(package.report_path).read_text(encoding="utf-8")
    assert "## Thesis and scope" in report
    assert "Deliberately out of scope" in report


# ---------------------------------------------------------------------------
# Enforcement: rules that claim to be automatic must actually be decided
# somewhere, and the focus family must fire on the failure that prompted it.
# ---------------------------------------------------------------------------


def test_every_auto_rule_has_a_detector_or_a_computed_check():
    """The hole that let S02 and C04 go unchecked for two rulesets.

    `rules_text` stamps `[auto]` on the rule and the validator prompt says to
    skip `[auto]` rules, while `run_detectors` skips anything with no detector.
    A rule in neither place is therefore checked by nobody, silently.
    """
    from ppn_blogger.detectors import COMPUTED_RULES

    orphans = [
        rule["id"]
        for rule in get_settings().all_rules()
        if rule.get("auto") and "detector" not in rule and rule["id"] not in COMPUTED_RULES
    ]
    assert not orphans, (
        f"{orphans} are marked auto with nothing behind them: give each a detector "
        "or add it to detectors.COMPUTED_RULES."
    )


def test_s02_and_c04_produce_code_findings():
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()
    lo = settings.structure["min_sections"]
    md = "# Title\n\n" + "".join(
        f"## Section {i}\n\nA very short section.\n\n" for i in range(lo - 2)
    )

    design = run_detectors(md, groups=settings.DESIGN_GROUPS, settings=settings)
    content = run_detectors(
        md,
        groups=settings.CONTENT_GROUPS,
        settings=settings,
        post_format="analysis",
        voice_mode="analysis",
    )

    assert "S02" in {f.rule_id for f in design.findings}
    assert "C04" in {f.rule_id for f in content.findings}


def _outline_for(titles, out_of_scope=()):
    from ppn_blogger.models import OutlineSection, PostOutline

    return PostOutline(
        thesis="One argument.",
        reader_promise="y",
        out_of_scope=list(out_of_scope),
        sections=[
            OutlineSection(title=t, makes_this_point="x", claim_ids=["C1"]) for t in titles
        ],
    )


def test_f03_fires_on_an_out_of_scope_heading():
    """The observed failure, encoded: four Business Central sections in a post
    about Copilot Studio orchestration."""
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()
    md = (
        "# Generative versus classic orchestration\n\n"
        "## How the two orchestrators differ\n\nBody.\n\n"
        "## What this means for Business Central\n\nBody.\n\n"
    )
    run = run_detectors(
        md,
        groups=settings.CONTENT_GROUPS,
        settings=settings,
        outline=_outline_for(
            ["How the two orchestrators differ"], out_of_scope=["Business Central"]
        ),
    )

    f03 = [f for f in run.findings if f.rule_id == "F03"]
    assert len(f03) == 1
    assert "Business Central" in f03[0].location


def test_f03_ignores_an_out_of_scope_mention_in_prose():
    """One clause naming the boundary is explicitly allowed; F02 owns the rest."""
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()
    md = (
        "# Generative versus classic orchestration\n\n"
        "## How the two orchestrators differ\n\n"
        "Unlike Business Central, this post stays on the orchestrator itself.\n\n"
    )
    run = run_detectors(
        md,
        groups=settings.CONTENT_GROUPS,
        settings=settings,
        outline=_outline_for(
            ["How the two orchestrators differ"], out_of_scope=["Business Central"]
        ),
    )

    assert not [f for f in run.findings if f.rule_id == "F03"]


def test_f04_reports_thin_sections_once_not_once_each():
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()
    md = "# Title\n\n" + "".join(f"## Section {i}\n\nToo short.\n\n" for i in range(6))
    run = run_detectors(
        md,
        groups=settings.CONTENT_GROUPS,
        settings=settings,
        outline=_outline_for([f"Section {i}" for i in range(6)]),
    )

    f04 = [f for f in run.findings if f.rule_id == "F04"]
    assert len(f04) == 1, "one aggregated finding, or the report drowns"
    assert "6 sections under" in f04[0].location


def test_f05_fires_when_the_headings_drift_from_the_outline():
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()
    md = "# Title\n\n## Planned one\n\nBody.\n\n## Never outlined\n\nBody.\n\n"
    run = run_detectors(
        md,
        groups=settings.CONTENT_GROUPS,
        settings=settings,
        outline=_outline_for(["Planned one", "Planned two"]),
    )

    f05 = [f for f in run.findings if f.rule_id == "F05"]
    assert len(f05) == 1
    assert "Never outlined" in f05[0].location
    assert "Planned two" in f05[0].location


def test_the_focus_rules_are_silent_without_an_outline():
    """TranslationGate runs the typography family with no outline in hand."""
    from ppn_blogger.detectors import run_detectors

    settings = get_settings()
    run = run_detectors(
        "# Title\n\n## One\n\nBody.\n\n", groups=settings.CONTENT_GROUPS, settings=settings
    )
    assert not [f for f in run.findings if f.rule_id.startswith("F")]


def test_the_stub_sample_fires_exactly_the_two_expected_findings():
    """The sample is short on purpose; the cost is C04 and F04, and no more.

    Asserted rather than tolerated, so the compromise stays a regression test of
    the new detectors instead of drifting into a silent oddity.
    """
    from ppn_blogger.detectors import run_detectors
    from ppn_blogger.testing import _SAMPLE_MARKDOWN, _dossier, _outline

    settings = get_settings()
    outline = _repaired(_outline(), _dossier(clean=True))
    ids = set()
    for groups, extra in (
        (settings.DESIGN_GROUPS, {}),
        (settings.CONTENT_GROUPS, {"post_format": "deep-dive", "voice_mode": "analysis"}),
    ):
        run = run_detectors(
            _SAMPLE_MARKDOWN,
            groups=groups,
            settings=settings,
            slug="dataverse-elastic-tables-tradeoffs",
            outline=outline,
            **extra,
        )
        ids |= {f.rule_id for f in run.findings}

    assert ids == {"C04", "F04"}, f"unexpected findings on the house sample: {sorted(ids)}"


def test_minor_findings_reach_the_writer():
    """They used to be filtered out, so a validator could deduct for a fix the
    writer never saw. One real run carried the same two minors through all
    three revision rounds."""
    from ppn_blogger.executors import _revision_text
    from ppn_blogger.models import RuleFinding, ValidationReport

    report = ValidationReport(
        validator="design",
        score=88,
        passed=False,
        findings=[
            RuleFinding(rule_id="S02", severity="major", problem="p", fix="f"),
            RuleFinding(rule_id="E05", severity="minor", problem="no internal links", fix="add two"),
            RuleFinding(rule_id="T03", severity="info", problem="p", fix="f"),
        ],
    )

    text = _revision_text([report])

    assert "S02" in text
    assert "E05" in text and "add two" in text
    assert "T03" not in text, "info never deducts and never blocks"
    assert text.index("S02") < text.index("E05"), "minors stay subordinate"


# ---------------------------------------------------------------------------
# Writing from the operator's own sources
# ---------------------------------------------------------------------------


def test_a_corpus_researcher_has_no_way_to_search():
    """The guard the whole corpus-only mode rests on.

    Filtering `RESEARCHER_TOOLS` would not be enough — `_searchable` is what
    attaches Foundry's server-side search — so what is asserted here is the
    absence of *any* route to the open web, not the absence of one function.
    """
    from ppn_blogger.agents import build_researcher

    clients = stub_clients()
    settings = get_settings()

    def tool_names(agent):
        return {getattr(t, "name", "") for t in agent.default_options.get("tools", [])}

    confined = tool_names(build_researcher(settings, clients, corpus_only=True))
    assert confined == {"fetch_page", "search_existing_posts", "today"}

    ordinary = tool_names(build_researcher(settings, clients))
    assert "search_microsoft_learn" in ordinary
    assert "read_feeds" in ordinary


@pytest.mark.asyncio
async def test_a_brief_becomes_a_topic_confined_to_its_links():
    """The interpreter decides what the post is; code decides what it may read.

    The stub answers with an invented `seed_sources` URL and a `post_format`
    outside the profile, so this walks the clamp rather than the happy path.
    """
    from ppn_blogger.workflows import topic_from_brief

    settings = get_settings()
    brief = (
        "Write up the elastic tables limits from https://learn.microsoft.com/a/elastic "
        "and https://example.com/notes, focusing on what stops working."
    )
    topic, corpus = await topic_from_brief(brief, clients=stub_clients())

    assert corpus == ["https://learn.microsoft.com/a/elastic", "https://example.com/notes"]
    assert topic.seed_sources == corpus, "the model's own URL must not survive"

    # A caller that worked the corpus out itself is believed, and the brief is
    # not read for links a second time — that is what keeps a resolved link from
    # arriving alongside the spelling it was resolved from.
    _, given = await topic_from_brief(
        brief, ["https://example.org/only-this"], clients=stub_clients()
    )
    assert given == ["https://example.org/only-this"]
    assert "https://example.invalid/never-supplied" not in topic.seed_sources
    assert topic.watch_area in {a["id"] for a in settings.watch_areas}
    assert topic.post_format in {f["id"] for f in settings.blog_profile["post_formats"]}
    assert topic.slug


def test_a_dossier_is_confined_to_the_corpus_deterministically():
    from ppn_blogger.executors import repair_corpus_citations
    from ppn_blogger.models import Citation, Claim, ResearchDossier

    dossier = ResearchDossier(
        topic_title="t",
        primary_keyword="k",
        post_format="analysis",
        summary="s",
        citations=[
            # Supplied as http, without the trailing slash: the same page.
            Citation(id="S1", title="mine", url="https://example.com/page/"),
            Citation(id="S2", title="not mine", url="https://elsewhere.example/x"),
        ],
        claims=[
            Claim(id="C1", statement="from the corpus", citation_ids=["S1", "S2"]),
            Claim(id="C2", statement="from nowhere", citation_ids=["S2"]),
            Claim(id="C3", statement="from memory", citation_ids=[]),
        ],
    )
    repaired = repair_corpus_citations(dossier, ["http://example.com/page"])

    assert [c.id for c in repaired.citations] == ["S1"]
    assert [c.id for c in repaired.claims] == ["C1"]
    assert repaired.claims[0].citation_ids == ["S1"]
    assert len(repaired.warnings) == 3


@pytest.mark.asyncio
async def test_a_corpus_run_writes_a_draft_that_cites_only_the_corpus(tmp_path, monkeypatch):
    settings = get_settings()
    for attr in ("topics_dir", "output_dir", "research_dir"):
        monkeypatch.setattr(settings.run, attr, tmp_path)

    from ppn_blogger.models import ResearchDossier
    from ppn_blogger.workflows import topic_from_brief

    clients = stub_clients(exercise_loops=False)
    corpus_url = "https://example.com/the-one-page"
    topic, corpus = await topic_from_brief(f"Write this up from {corpus_url}", clients=clients)

    package = await write_post(
        topic, clients=clients, push_to_wordpress=False, source_corpus=corpus
    )
    assert isinstance(package, PostPackage)

    saved = ResearchDossier.model_validate_json(
        Path(package.dossier_path).read_text(encoding="utf-8")
    )
    assert [c.url for c in saved.citations] == [corpus_url]
    assert saved.warnings, "a dropped citation must say so on the artefact"
    # Nothing may be left resting on a source the operator never supplied.
    kept = {c.id for c in saved.citations}
    assert all(set(claim.citation_ids) <= kept for claim in saved.claims)
