from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from god_news.api.media_catalog_routes import router as media_catalog_router
from god_news.api.operations_routes import router as operations_router
from god_news.api.routes import router
from god_news.api.runtime_routes import router as runtime_router
from god_news.api.schemas import ProblemDetail
from god_news.api.source_media_routes import router as source_media_router
from god_news.api.source_run_routes import router as source_run_router
from god_news.api.source_transcription_routes import router as source_transcription_router
from god_news.api.video_routes import router as video_router
from god_news.api.visual_asset_routes import router as visual_asset_router
from god_news.api.visual_discovery_routes import router as visual_discovery_router
from god_news.config import Settings, get_settings
from god_news.container import AppContainer, build_container
from god_news.errors import GodNewsError
from god_news.logging import configure_logging, reset_trace_id, set_trace_id, trace_id_var

logger = logging.getLogger(__name__)
ContainerFactory = Callable[[Settings], Awaitable[AppContainer]]


def create_app(
    settings: Settings | None = None,
    *,
    container_factory: ContainerFactory = build_container,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await container_factory(resolved_settings)
        app.state.container = container
        try:
            await container.start()
            yield
        finally:
            await container.aclose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        description="Review-gated multilingual content-to-audio pipeline.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def trace_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        candidate = request.headers.get("X-Trace-ID")
        try:
            trace_id = str(UUID(candidate)) if candidate else str(uuid4())
        except ValueError:
            trace_id = str(uuid4())
        token = set_trace_id(trace_id)
        try:
            response = await call_next(request)
            response.headers["X-Trace-ID"] = trace_id
            return response
        finally:
            reset_trace_id(token)

    @app.exception_handler(GodNewsError)
    async def handle_domain_error(request: Request, exc: GodNewsError) -> JSONResponse:
        del request
        logger.warning("request failed: %s", exc.code)
        problem = ProblemDetail(
            code=exc.code,
            message=exc.public_message,
            trace_id=trace_id_var.get(),
            story_id=exc.story_id,
        )
        return JSONResponse(status_code=exc.status_code, content=problem.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        issues = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error.get("loc", ())) or "request"
            error_type = str(error.get("type", "invalid"))
            issues.append(f"{location} [{error_type}]")
        issue_text = "; ".join(issues) or "request [invalid]"
        problem = ProblemDetail(
            code="request_validation_failed",
            message=(
                f"请求不符合当前接口契约: {issue_text}。"
                "如果刚更新过代码, 请重启后端后重试。"
            ),
            trace_id=trace_id_var.get(),
        )
        return JSONResponse(status_code=422, content=problem.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        del request
        logger.exception("unhandled request failure", exc_info=exc)
        problem = ProblemDetail(
            code="internal_error",
            message="An unexpected internal error occurred.",
            trace_id=trace_id_var.get(),
        )
        return JSONResponse(status_code=500, content=problem.model_dump(mode="json"))

    app.include_router(router, prefix="/api/v1")
    app.include_router(runtime_router, prefix="/api/v1")
    app.include_router(operations_router, prefix="/api/v1")
    app.include_router(source_run_router, prefix="/api/v1")
    app.include_router(source_media_router, prefix="/api/v1")
    app.include_router(source_transcription_router, prefix="/api/v1")
    app.include_router(visual_asset_router, prefix="/api/v1")
    app.include_router(visual_discovery_router, prefix="/api/v1")
    app.include_router(media_catalog_router, prefix="/api/v1")
    app.include_router(video_router, prefix="/api/v1")
    return app
