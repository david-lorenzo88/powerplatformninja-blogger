"""Cover image generation.

The cover is **pure generated artwork**. Nothing is composited on top — no title,
no chip, no logo. The model renders a neon-lit graphic built from the post's own
subject matter and that is the finished image.

Three request shapes, selected automatically from ``COVER_PROVIDER`` and the
model name — setting ``COVER_MODEL`` is normally enough.

* **MAI** — ``MAI-Image-2.5-Pro`` and siblings are Microsoft's own image models
  and do **not** speak the OpenAI images API. They use
  ``/mai/v1/images/generations`` with integer ``width``/``height``, no
  ``quality``, no ``n``, and a hard ceiling of 1,048,576 total pixels. Anything
  larger is fitted down here rather than rejected by the service.
* **OpenAI-compatible on Azure** — ``gpt-image-2`` (GA), ``gpt-image-1`` and
  friends (limited access, registration required), ``FLUX-1.1-pro`` (serverless,
  every region, no registration). ``dall-e-3`` was retired on 4 March 2026.
  These use ``/openai/v1/images/generations`` with a ``size`` string.
* **OpenAI direct** — api.openai.com with ``OPENAI_API_KEY``. Note a ChatGPT
  Plus/Pro subscription is **not** API access, and the GPT Image models need
  Organization Verification first.

A cover failure never costs you the draft: it is caught, recorded on the
``CoverImage`` as ``error``, and the pipeline continues.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from .models import CoverImage, Draft
from .settings import Settings, get_settings

logger = logging.getLogger("ppn.covers")

# Models that do not accept the OpenAI `quality` parameter.
_NO_QUALITY_PREFIXES = ("flux", "stable", "sd3")

# MAI image models cap total pixels. Exceeding it is a 400, so fit instead.
MAI_MAX_PIXELS = 1_048_576
MAI_MIN_EDGE = 768


def fit_to_mai_limits(width: int, height: int) -> tuple[int, int]:
    """Scale a requested size into MAI's pixel budget, keeping the aspect ratio.

    Dimensions are rounded down to multiples of 16 (which every provider here is
    happy with) and floored at MAI's 768px minimum edge.
    """
    import math

    if width * height > MAI_MAX_PIXELS:
        scale = math.sqrt(MAI_MAX_PIXELS / (width * height))
        width, height = int(width * scale), int(height * scale)

    width = max(MAI_MIN_EDGE, width // 16 * 16)
    height = max(MAI_MIN_EDGE, height // 16 * 16)

    # Flooring to the minimum edge can push us back over the cap on extreme
    # aspect ratios; shrink the longer side until it fits.
    while width * height > MAI_MAX_PIXELS and max(width, height) > MAI_MIN_EDGE:
        if width >= height:
            width -= 16
        else:
            height -= 16
    return width, height


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(draft: Draft, settings: Settings) -> str:
    """Compose the image prompt from art direction, palette and the post's concept."""
    cover_cfg = settings.blog_profile.get("cover", {})
    art_direction = (cover_cfg.get("art_direction") or "").strip()
    negative = (cover_cfg.get("negative") or "").strip()
    palette = cover_cfg.get("palette", {}) or {}
    colours = palette.get(draft.category) or palette.get("default") or ""

    concept = (draft.cover_concept or "").strip()
    if not concept:
        # Fall back to the post's own subject rather than something generic.
        concept = (
            f"A neon-lit graphic evoking {draft.primary_keyword or draft.title}"
            f"{f' in the context of {draft.category}' if draft.category else ''}"
        )

    parts = [art_direction, f"Subject of the graphic: {concept}"]
    if colours:
        parts.append(f"Colour scheme: {colours}.")
    if draft.category:
        parts.append(f"Topic area: {draft.category}.")
    if negative:
        parts.append(f"Absolutely do not include: {negative}")
    return "\n\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _api_key(settings: Settings) -> str:
    """API key, or a fresh Entra token used as the bearer credential."""
    if settings.cover.api_key:
        return settings.cover.api_key
    from .clients import _credential

    token = _credential().get_token("https://cognitiveservices.azure.com/.default")
    return token.token


def image_client(settings: Settings | None = None) -> Any:
    """Image client for whichever provider is configured.

    ``COVER_PROVIDER=openai`` talks to api.openai.com directly with an API key.
    Note that an OpenAI **API** key is not the same thing as a ChatGPT Plus or
    Pro subscription — the API is billed separately, and the GPT Image models
    additionally require Organization Verification in the developer console.

    ``COVER_PROVIDER=foundry`` (default) uses the OpenAI-compatible route on your
    Azure resource with Entra or an API key. MAI models do not go through this
    client at all — see :func:`_generate_mai`.
    """
    from openai import AsyncOpenAI

    settings = settings or get_settings()
    cover = settings.cover

    if cover.uses_openai:
        if not cover.openai_api_key:
            raise RuntimeError(
                "COVER_PROVIDER=openai but OPENAI_API_KEY is not set. Create a key at "
                "platform.openai.com — a ChatGPT Plus/Pro subscription does not include "
                "API access; the API is funded and billed separately."
            )
        kwargs: dict[str, Any] = {"api_key": cover.openai_api_key, "timeout": 240.0}
        if cover.openai_org:
            kwargs["organization"] = cover.openai_org
        return AsyncOpenAI(**kwargs)

    root = cover.endpoint or cover.derived_endpoint
    if not root:
        raise RuntimeError(
            "Cannot work out the image endpoint. Set COVER_ENDPOINT to your resource root, "
            "e.g. https://<resource>.services.ai.azure.com"
        )
    return AsyncOpenAI(
        base_url=f"{root}/openai/v1",
        api_key=_api_key(settings),
        default_query={"api-version": cover.api_version},
        timeout=240.0,
    )


IMAGE_HINTS = ("image", "dall", "flux", "stable", "sd3", "mai-")


async def list_image_deployments(settings: Settings | None = None) -> list[str]:
    """Best-effort list of models visible to the configured provider.

    Used by ``ppn models`` so you can see what you actually have instead of
    guessing at a model name. On OpenAI the catalogue is huge, so it is filtered
    to things that look like image models; on Foundry you get every deployment,
    because the list is short and the names are yours.
    """
    settings = settings or get_settings()
    # The MAI route has no model list, but MAI deployments still appear on the
    # OpenAI-compatible route of the same resource — and image_client() already
    # returns that client for every non-OpenAI provider.
    client = image_client(settings)
    result = await client.models.list()
    names = {getattr(m, "id", "") for m in result.data if getattr(m, "id", "")}
    if settings.cover.uses_openai:
        names = {n for n in names if any(h in n.lower() for h in IMAGE_HINTS)}
    return sorted(names)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


async def _generate_mai(prompt: str, settings: Settings) -> bytes:
    """Call the MAI images route, which is not OpenAI-compatible."""
    import httpx

    cover = settings.cover
    root = cover.endpoint or cover.derived_endpoint
    if not root:
        raise RuntimeError(
            "Cannot work out the image endpoint. Set COVER_ENDPOINT to your resource root, "
            "e.g. https://<resource>.services.ai.azure.com"
        )

    requested = cover.dimensions
    width, height = fit_to_mai_limits(*requested)
    if (width, height) != requested:
        logger.info(
            "MAI caps images at %s pixels, so %sx%s was fitted to %sx%s (same aspect ratio).",
            f"{MAI_MAX_PIXELS:,}", requested[0], requested[1], width, height,
        )

    headers = {"Content-Type": "application/json"}
    if cover.api_key:
        headers["api-key"] = cover.api_key
    else:
        from .clients import _credential

        token = _credential().get_token("https://cognitiveservices.azure.com/.default")
        headers["Authorization"] = f"Bearer {token.token}"

    payload = {"model": cover.model, "prompt": prompt, "width": width, "height": height}
    url = f"{root}/mai/v1/images/generations"

    logger.info("generating cover art with %s via MAI (%sx%s)", cover.model, width, height)
    async with httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=20.0)) as http:
        response = await http.post(
            url, json=payload, headers=headers, params={"api-version": cover.api_version}
        )
    if response.status_code >= 400:
        raise RuntimeError(f"MAI images returned {response.status_code}: {response.text[:400]}")

    data = response.json().get("data") or []
    for item in data:
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            async with httpx.AsyncClient(timeout=120.0) as http:
                fetched = await http.get(item["url"])
                fetched.raise_for_status()
                return fetched.content
    raise RuntimeError(f"MAI images returned no image data: {response.text[:300]}")


async def generate_art(prompt: str, settings: Settings) -> bytes:
    if settings.cover.uses_mai:
        return await _generate_mai(prompt, settings)

    client = image_client(settings)
    model = settings.cover.model

    kwargs: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
    if settings.cover.size and settings.cover.size != "auto":
        kwargs["size"] = settings.cover.size
    if settings.cover.quality and not model.lower().startswith(_NO_QUALITY_PREFIXES):
        kwargs["quality"] = settings.cover.quality

    logger.info(
        "generating cover art with %s via %s (%s)",
        model, settings.cover.provider, settings.cover.size,
    )
    response = await client.images.generate(**kwargs)

    item = response.data[0]
    if getattr(item, "b64_json", None):
        return base64.b64decode(item.b64_json)
    if getattr(item, "url", None):
        import httpx

        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.get(item.url)
            resp.raise_for_status()
            return resp.content
    raise RuntimeError("The image API returned neither b64_json nor url.")


def _dimensions(png: bytes) -> tuple[int, int]:
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png)) as img:
            return img.size
    except Exception:  # noqa: BLE001 - dimensions are cosmetic
        return (0, 0)


async def build_cover(draft: Draft, settings: Settings | None = None) -> CoverImage:
    """Generate the cover. Never raises — failures land in ``error``."""
    settings = settings or get_settings()
    prompt = build_prompt(draft, settings)
    cover = CoverImage(
        prompt=prompt,
        alt_text=f"Cover artwork for the article “{draft.title}”",
        model=settings.cover.model,
    )

    if not settings.cover.enabled:
        cover.error = "Cover generation disabled (COVER_ENABLED=false)."
        return cover

    try:
        art = await generate_art(prompt, settings)
    except Exception as exc:  # noqa: BLE001 - a missing cover must not lose the draft
        cover.error = f"{type(exc).__name__}: {str(exc)[:400]}"
        logger.error("cover art generation failed: %s", cover.error)
        lowered = cover.error.lower()
        if settings.cover.uses_openai:
            if "verif" in lowered or "403" in lowered:
                logger.error(
                    "OpenAI refused the request. GPT Image models need Organization "
                    "Verification: platform.openai.com → Settings → Organization → General.",
                )
            elif "401" in lowered or "api key" in lowered:
                logger.error(
                    "OpenAI rejected the key. Remember a ChatGPT Plus/Pro subscription is not "
                    "API access — create and fund a key at platform.openai.com.",
                )
            elif "quota" in lowered or "billing" in lowered or "429" in lowered:
                logger.error("OpenAI reports no credit or a rate limit on this key.")
        elif "deploymentnotfound" in lowered or "404" in lowered:
            logger.error(
                "No deployment named %r reachable on the %s route. Run `ppn models` to list "
                "what is deployed on that resource.",
                settings.cover.model,
                settings.cover.route,
            )
        elif "400" in lowered and settings.cover.uses_mai:
            logger.error(
                "MAI rejected the request. It caps images at %s total pixels with a %spx "
                "minimum edge — check COVER_SIZE (%s).",
                f"{MAI_MAX_PIXELS:,}", MAI_MIN_EDGE, settings.cover.size,
            )
        return cover

    directory = settings.run.output_dir / "covers"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{draft.slug or 'cover'}.png"
    path.write_bytes(art)

    cover.path = str(path)
    cover.width, cover.height = _dimensions(art)
    logger.info("cover written to %s (%sx%s)", path, cover.width, cover.height)
    return cover
