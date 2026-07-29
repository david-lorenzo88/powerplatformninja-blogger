"""End-to-end checks that run entirely offline against the stub client."""

from __future__ import annotations

from pathlib import Path

import pytest

from ppn_blogger.models import PostPackage, TopicSuggestionSet
from ppn_blogger.settings import get_settings
from ppn_blogger.testing import stub_clients
from ppn_blogger.workflows import discover_topics, write_post


@pytest.mark.asyncio
async def test_topic_discovery_produces_suggestions(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings.run, "topics_dir", tmp_path)
    result = await discover_topics(clients=stub_clients())
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


def test_rules_load_and_are_non_empty():
    settings = get_settings()
    rules = settings.all_rules()
    assert len(rules) >= 20
    # v2 ruleset: six families instead of three.
    assert {r["group"] for r in rules} == {
        "honesty", "typography", "voice", "content", "structure", "seo"
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
    """The new ruleset loads and every one of its 21 detectors compiles."""
    import re

    from ppn_blogger.detectors import compile_all

    settings = get_settings()
    compiled = compile_all(settings)
    assert len(compiled) == 21, f"expected 21 detectors, got {len(compiled)}"
    assert all(isinstance(p, re.Pattern) for p in compiled.values())
    # The six families are all present.
    assert {r["group"] for r in settings.all_rules()} == {
        "honesty", "typography", "voice", "content", "structure", "seo"
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
