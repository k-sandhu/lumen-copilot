"""Exercise the actual offline gateway fixture through pytest teardown (#814)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_offline_gateway_fixture_retires_clients_before_loop_shutdown(tmp_path: Path) -> None:
    """Mocked provider I/O still allocates a client owned by each test loop."""
    report = tmp_path / "ownership.json"
    (tmp_path / "conftest.py").write_text(
        "from tests.test_llm_gateway import _fresh_embed_cache\n"
        "from tests.test_embedding_client_lifecycle import loop_clients\n"
        "import json\n"
        "from pathlib import Path\n"
        "observed = []\n"
        "def pytest_sessionfinish(session, exitstatus):\n"
        f"    Path({str(report)!r}).write_text(json.dumps([\n"
        "        {'closed': c.closed, 'loop_closed': c.loop.is_closed()}\n"
        "        for c in observed]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    (tmp_path / "test_owned_clients.py").write_text(
        "import pytest\n"
        "import conftest\n"
        "from app.llm import LLMGateway\n"
        "from app.core.errors import DependencyError\n"
        "from tests.test_llm_gateway import _settings\n"
        "@pytest.mark.asyncio\n"
        "@pytest.mark.parametrize('dimensions', [1, 2048])\n"
        "async def test_offline_provider(loop_clients, dimensions):\n"
        "    gateway = LLMGateway(_settings(LLM_EMBEDDING_DIMENSIONS=str(dimensions)))\n"
        "    if dimensions == 1:\n"
        "        assert len(await gateway.embed(['offline success'])) == 1\n"
        "    else:\n"
        "        with pytest.raises(DependencyError) as failure:\n"
        "            await gateway.embed(['offline dimension drift'])\n"
        "        assert failure.value.code == 'embedding_dimension_mismatch'\n"
        "    conftest.observed.extend(loop_clients)\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "pyproject.toml",
            "-o",
            "addopts=",
            "--confcutdir",
            str(tmp_path),
            str(tmp_path / "test_owned_clients.py"),
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    clients = json.loads(report.read_text(encoding="utf-8"))
    assert len(clients) == 2, "each offline test loop must allocate one client"
    assert all(client["loop_closed"] for client in clients)
    assert all(
        client["closed"] for client in clients
    ), "offline fixture left loop-owned clients open after their loops closed"
