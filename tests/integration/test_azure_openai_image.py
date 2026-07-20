import os
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from agent_app.config import Settings
from agent_app.engines.base import CancellationSignal
from agent_app.engines.maf import create_maf_engine
from agent_app.image_inputs import NormalizedImage

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AZURE_INTEGRATION") != "1",
    reason="Set RUN_AZURE_INTEGRATION=1 to call the configured Azure OpenAI deployment",
)


def synthetic_image() -> NormalizedImage:
    image = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 45, 240, 135), fill="blue")
    output = BytesIO()
    image.save(output, format="JPEG", quality=85)
    return NormalizedImage(
        content=output.getvalue(),
        media_type="image/jpeg",
        width=320,
        height=180,
    )


async def test_real_maf_image_run_describes_synthetic_shape() -> None:
    settings = Settings(
        engine="maf",
        approval_table_endpoint="https://integration.table.core.windows.net",
        artifact_blob_endpoint="https://integration.blob.core.windows.net",
        artifact_public_base_url="https://agent.example.invalid",
        tenant_id="integration-tenant",
        _env_file=None,
    )
    engine = create_maf_engine(settings)
    updates: list[str] = []

    try:
        async for delta in engine.stream_image(
            instruction=(
                "Describe the dominant colored geometric shape in this image "
                "in one short sentence."
            ),
            image=synthetic_image(),
            conversation_id="image-integration-test",
            cancellation=CancellationSignal(),
        ):
            assert delta.text
            updates.append(delta.text)
    finally:
        await engine.close()

    response = "".join(updates).casefold()
    assert updates
    assert "blue" in response
    assert "rectangle" in response or "rectangular" in response
