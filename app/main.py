"""KIC SignalForge - FastAPI 애플리케이션 진입점."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router
from .config import get_settings
from .observability import setup_logging

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()

    app = FastAPI(
        title="KIC SignalForge",
        description=(
            "공개 금융·기업 자료를 바탕으로 근거/반대 근거/불확실성을 함께 제시하는 "
            "투자 리서치 보조 Agent (수집·검증 전용, 투자 권유 아님)"
        ),
        version="0.1.0",
    )
    app.include_router(router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(str(STATIC_DIR / "index.html"))

    return app


app = create_app()


def main() -> None:  # pragma: no cover - 실행 진입점
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
