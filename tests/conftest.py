import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'platform.db'}",
        import_root=tmp_path / "imports",
        batch_size=2,
        demo_base_url="http://source.test",
        http_allowed_hosts=("source.test",),
    )

    def handler(request):
        assert request.url.host == "source.test"
        return httpx.Response(
            200, json=json.loads((settings.import_root / "alarms.json").read_text(encoding="utf-8"))
        )

    app = create_app(settings, httpx.MockTransport(handler))
    with TestClient(app) as client:
        yield {
            "app": app,
            "client": client,
            "db": app.state.db,
            "service": app.state.service,
            "settings": settings,
        }
