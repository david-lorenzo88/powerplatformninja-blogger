"""The newsletter endpoints, over real HTTP.

This file exists because of a bug that reached production. Every newsletter test
called ``newsletters.create()`` directly, so the *endpoint* was never exercised —
and the endpoint passed ``name`` both positionally and in the keyword splat,
which is a TypeError and a bare 500 with nothing useful in it. The store was
correct the whole time; only the layer that assembles the arguments was wrong.

The lesson generalises: a store test and an endpoint test are not the same test,
and the glue that unpacks a request body is exactly where this class of bug
lives.
"""

from __future__ import annotations

import pytest


@pytest.fixture
async def api(monkeypatch, database_url):
    monkeypatch.setenv("PPN_MAX_CONCURRENT_RUNS", "2")

    from ppn_blogger.config_source import set_config_source
    from ppn_blogger.server import runs

    await runs.reset_manager()

    import httpx

    from ppn_blogger.server.app import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with app.router.lifespan_context(app):
            yield client

    await runs.reset_manager()
    set_config_source(None)


async def test_creating_a_newsletter_over_http(api) -> None:
    """The regression. `name` arrived twice and the endpoint 500'd."""
    response = await api.post(
        "/api/news/newsletters",
        json={"name": "AI Papers", "description": "the latest AI papers"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "AI Papers"
    assert body["slug"] == "ai-papers"
    assert body["schedule_kind"] == "manual"
    assert body["enabled"] is True


async def test_creating_with_every_field_the_form_can_send(api) -> None:
    """The form posts more than a name; the splat has to survive all of it."""
    response = await api.post(
        "/api/news/newsletters",
        json={
            "name": "Everything",
            "description": "d",
            "schedule_kind": "weekly",
            "weekday": 2,
            "hour_local": 7,
            "minute_local": 30,
            "timezone": "Europe/Madrid",
            "lookback_hours": 72,
            "max_items": 8,
            "min_items": 2,
            "max_per_feed": 2,
            "audience": "practitioners",
            "tone": "dry",
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["schedule_kind"] == "weekly" and body["weekday"] == 2
    assert body["upcoming"], "a scheduled newsletter must report its next fire times"


async def test_a_blank_name_is_a_422_not_a_500(api) -> None:
    for name in ("", "   "):
        response = await api.post("/api/news/newsletters", json={"name": name})
        assert response.status_code == 422, response.text
        assert "needs a name" in response.json()["detail"]


async def test_a_duplicate_name_is_a_conflict(api) -> None:
    assert (await api.post("/api/news/newsletters", json={"name": "Weekly"})).status_code == 201
    duplicate = await api.post("/api/news/newsletters", json={"name": "weekly"})
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]


async def test_an_unknown_schedule_kind_is_a_422(api) -> None:
    letter = (await api.post("/api/news/newsletters", json={"name": "Weekly"})).json()
    response = await api.patch(
        f"/api/news/newsletters/{letter['id']}", json={"schedule_kind": "hourly-ish"}
    )
    assert response.status_code == 422
    assert "Unknown schedule kind" in response.json()["detail"]


async def test_patching_without_a_name_keeps_the_name(api) -> None:
    """Editing the schedule must not blank the name.

    The request model defaults `name` to "", so a PATCH that does not mean to
    rename has to be told apart from one that does.
    """
    letter = (await api.post("/api/news/newsletters", json={"name": "Keep me"})).json()
    patched = (
        await api.patch(f"/api/news/newsletters/{letter['id']}", json={"lookback_hours": 48})
    ).json()
    assert patched["name"] == "Keep me"
    assert patched["lookback_hours"] == 48


async def test_newsletter_crud_round_trip(api) -> None:
    created = (await api.post("/api/news/newsletters", json={"name": "Temp"})).json()
    assert len((await api.get("/api/news/newsletters")).json()) == 1

    assert (await api.get(f"/api/news/newsletters/{created['id']}")).status_code == 200
    assert (await api.get("/api/news/newsletters/999999")).status_code == 404

    assert (await api.delete(f"/api/news/newsletters/{created['id']}")).status_code == 200
    assert (await api.get("/api/news/newsletters")).json() == []
    assert (await api.delete("/api/news/newsletters/999999")).status_code == 404


async def test_preview_of_an_empty_newsletter_explains_itself(api) -> None:
    letter = (await api.post("/api/news/newsletters", json={"name": "Empty"})).json()
    body = (await api.get(f"/api/news/newsletters/{letter['id']}/preview")).json()
    assert body["candidates"] == []
    assert "no feed groups" in body["reason"]
    assert (await api.get("/api/news/newsletters/999999/preview")).status_code == 404


async def test_no_newsletter_route_redirects(api) -> None:
    """Same rule as the rest of the router: a trailing slash 404s, never 307s."""
    for path in ("/api/news/newsletters", "/api/news/issues"):
        assert (await api.get(path)).status_code == 200
        assert (await api.get(path + "/")).status_code == 404
