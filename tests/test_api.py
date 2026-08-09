from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from god_news.api.app import create_app

from .conftest import Stack


@pytest.mark.asyncio
async def test_api_drives_the_full_offline_pipeline(stack: Stack) -> None:
    async def factory(settings):  # type: ignore[no-untyped-def]
        del settings
        return stack.container

    app = create_app(stack.settings, container_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            live = await client.get("/api/v1/health/live")
            assert live.status_code == 200
            UUID(live.headers["X-Trace-ID"])

            ready = await client.get("/api/v1/health/ready", headers={"X-Trace-ID": "not-a-uuid"})
            assert ready.status_code == 200
            UUID(ready.headers["X-Trace-ID"])

            created = await client.post(
                "/api/v1/stories",
                json={
                    "source": {
                        "kind": "text",
                        "title": "API fixture",
                        "language": "en",
                        "text": "A source story with names, dates, and verifiable quantities.",
                    },
                    "target_language": "zh-CN",
                },
            )
            assert created.status_code == 201
            story = created.json()
            assert story["status"] == "PENDING_FIRST_REVIEW"
            assert story["translation"]["screening"]["category"] == "kindness"
            story_id = story["story_id"]

            retranslated = await client.post(
                f"/api/v1/stories/{story_id}/retranslate",
                json={"expected_story_version": story["version"]},
            )
            assert retranslated.status_code == 200
            story = retranslated.json()
            assert story["status"] == "PENDING_FIRST_REVIEW"
            assert stack.generator.translation_calls == 2

            first = await client.post(
                f"/api/v1/stories/{story_id}/reviews/first",
                json={
                    "expected_story_version": story["version"],
                    "decision": "approve",
                    "reviewer_id": "editor-1",
                },
            )
            assert first.status_code == 200
            assert first.json()["status"] == "SCRIPT_READY"
            assert first.json()["audio"] is None

            script_review = await client.post(
                f"/api/v1/stories/{story_id}/reviews/script",
                json={
                    "expected_story_version": first.json()["version"],
                    "decision": "approve",
                    "reviewer_id": "script-editor",
                },
            )
            assert script_review.status_code == 200
            assert script_review.json()["status"] == "PENDING_TTS"

            synthesized = await client.post(
                f"/api/v1/stories/{story_id}/synthesize",
                json={"expected_story_version": script_review.json()["version"]},
            )
            assert synthesized.status_code == 200
            assert synthesized.json()["status"] == "PENDING_SECOND_REVIEW"
            segment_id = synthesized.json()["audio"]["clips"][0]["segment_id"]

            metrics = await client.get("/api/v1/metrics/classification")
            assert metrics.status_code == 200
            assert metrics.json() == {
                "reviewed_count": 1,
                "accepted_count": 1,
                "accuracy": 1.0,
            }

            preview = await client.get(f"/api/v1/stories/{story_id}/audio/{segment_id}")
            assert preview.status_code == 200
            assert preview.content.startswith(b"RIFF")

            blocked_manifest = await client.get(f"/api/v1/stories/{story_id}/production-manifest")
            assert blocked_manifest.status_code == 409

            second = await client.post(
                f"/api/v1/stories/{story_id}/reviews/second",
                json={
                    "expected_story_version": synthesized.json()["version"],
                    "decision": "approve",
                    "reviewer_id": "editor-2",
                },
            )
            assert second.status_code == 200
            assert second.json()["status"] == "DONE"

            listed = await client.get("/api/v1/stories", params={"status": "DONE"})
            assert listed.status_code == 200
            assert [item["story_id"] for item in listed.json()] == [story_id]

            fetched = await client.get(f"/api/v1/stories/{story_id}")
            assert fetched.status_code == 200
            assert fetched.json()["version"] == second.json()["version"]

            reviews = await client.get(f"/api/v1/stories/{story_id}/reviews")
            transitions = await client.get(f"/api/v1/stories/{story_id}/transitions")
            assert len(reviews.json()) == 3
            assert len(transitions.json()) == 8

            openapi = await client.get("/openapi.json")
            assert openapi.status_code == 200
            assert (
                openapi.json()["paths"]["/api/v1/stories/{story_id}/reviews/script"]["post"][
                    "operationId"
                ]
                == "submitScriptReview"
            )
            assert (
                openapi.json()["paths"]["/api/v1/stories/{story_id}/synthesize"]["post"][
                    "operationId"
                ]
                == "synthesizeStory"
            )
            assert (
                openapi.json()["paths"]["/api/v1/stories/{story_id}/retranslate"]["post"][
                    "operationId"
                ]
                == "retranslateStory"
            )
            create_schema = openapi.json()["components"]["schemas"]["CreateStoryRequest"]
            assert create_schema["required"] == ["source", "target_language"]
            assert set(create_schema["properties"]) == {"source", "target_language"}

            cannot_resume = await client.post(f"/api/v1/stories/{story_id}/resume")
            assert cannot_resume.status_code == 409

            manifest = await client.get(f"/api/v1/stories/{story_id}/production-manifest")
            assert manifest.status_code == 200
            manifest_body = manifest.json()
            assert manifest_body["schema_version"] == "2.0"
            assert manifest_body["spoken_language"] == "zh-CN"
            assert manifest_body["timeline"][0]["spoken_text"]
            assert manifest_body["timeline"][0]["captions"][0]["kind"] == "verbatim"
            assert "language" not in manifest_body
            assert "text" not in manifest_body["timeline"][0]

            missing = await client.get(f"/api/v1/stories/{uuid4()}")
            assert missing.status_code == 404


@pytest.mark.asyncio
async def test_api_returns_stable_validation_problem(stack: Stack) -> None:
    async def factory(settings):  # type: ignore[no-untyped-def]
        del settings
        return stack.container

    app = create_app(stack.settings, container_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/stories", json={"source": {}})
            obsolete_preferences = await client.post(
                "/api/v1/stories",
                json={
                    "source": {
                        "kind": "text",
                        "title": "Boundary test",
                        "text": "Preferences belong to first review.",
                    },
                    "target_language": "zh-CN",
                    "style": "must not be accepted at creation",
                },
            )
    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"
    assert obsolete_preferences.status_code == 422
    assert obsolete_preferences.json()["code"] == "request_validation_failed"
