from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.mark.parametrize("stage", ["run", "enter"])
@pytest.mark.parametrize("required", [False, True])
def test_gateway_handles_mcp_startup_failure(db_file, monkeypatch, stage, required):
    monkeypatch.setenv("GATEWAY_MCP_REQUIRED", "true" if required else "false")
    failure = RuntimeError("MCP session manager failed")
    mcp = MagicMock()
    if stage == "run":
        mcp.session_manager.run.side_effect = failure
    else:
        mcp.session_manager.run.return_value.__aenter__.side_effect = failure

    async def mcp_http(request):
        pytest.fail("MCP endpoint was dispatched without a running session manager")

    monkeypatch.setattr(main, "build_mcp", lambda state: (mcp, mcp_http))
    app = main.create_app(testing=True)
    if required:
        with pytest.raises(RuntimeError, match="MCP session manager failed"):
            with TestClient(app):
                pass
        return

    with TestClient(app) as client:
        assert client.get("/live").status_code == 200
        assert client.post("/mcp", json={}).status_code == 404
