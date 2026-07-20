import os
from time import monotonic

import pytest

from agent_app.config import Settings
from agent_app.engines.base import CancellationSignal
from agent_app.engines.maf import create_maf_engine

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AZURE_INTEGRATION") != "1",
    reason="Set RUN_AZURE_INTEGRATION=1 to call the configured Azure OpenAI deployment",
)


async def test_real_maf_run_yields_native_updates_before_completion() -> None:
    settings = Settings(
        engine="maf",
        approval_table_endpoint="https://integration.table.core.windows.net",
        artifact_blob_endpoint="https://integration.blob.core.windows.net",
        artifact_public_base_url="https://agent.example.invalid",
        tenant_id="integration-tenant",
        _env_file=None,
    )
    engine = create_maf_engine(settings)
    started = monotonic()
    first_update: float | None = None
    update_count = 0

    try:
        async for delta in engine.stream(
            "Reply with one short sentence about safe software testing.",
            "integration-test",
            CancellationSignal(),
        ):
            if first_update is None:
                first_update = monotonic()
            update_count += 1
            assert delta.text
    finally:
        await engine.close()

    completed = monotonic()
    assert first_update is not None
    assert started < first_update < completed
    assert update_count >= 2
