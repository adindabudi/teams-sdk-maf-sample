import asyncio

from agent_app.config import Settings
from agent_app.teams_app import create_teams_runtime
from agent_app.telemetry import configure_telemetry


async def run() -> None:
    settings = Settings()
    configure_telemetry(settings.applicationinsights_connection_string)
    runtime = create_teams_runtime(settings)
    try:
        await runtime.app.start(settings.port)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(run())
