# Third-Party Notices

This project uses the following direct runtime dependencies. The versions are locked in `uv.lock`; transitive packages and integrity hashes are recorded there as well.

| Component | Version | License | Source |
| --- | --- | --- | --- |
| Microsoft Agent Framework Core | 1.11.0 | MIT | [microsoft/agent-framework](https://github.com/microsoft/agent-framework) |
| Microsoft Agent Framework OpenAI | 1.10.1 | MIT | [microsoft/agent-framework](https://github.com/microsoft/agent-framework) |
| Azure Data Tables | 12.7.0 | MIT | [Azure SDK for Python](https://github.com/Azure/azure-sdk-for-python) |
| Azure Identity | 1.25.3 | MIT | [Azure SDK for Python](https://github.com/Azure/azure-sdk-for-python) |
| Azure Monitor OpenTelemetry | 1.8.9 | MIT | [Azure SDK for Python](https://github.com/Azure/azure-sdk-for-python) |
| Azure Storage Blob | 12.30.0 | MIT | [Azure SDK for Python](https://github.com/Azure/azure-sdk-for-python) |
| HTTPX | 0.28.1 | BSD-3-Clause | [encode/httpx](https://github.com/encode/httpx) |
| Microsoft Teams SDK for Python | 2.0.14 | MIT | [microsoft/teams.py](https://github.com/microsoft/teams.py) |
| Pillow | 12.3.0 | MIT-CMU | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) |
| Pydantic Settings | 2.14.2 | MIT | [pydantic/pydantic-settings](https://github.com/pydantic/pydantic-settings) |
| Tenacity | 9.1.4 | Apache-2.0 | [jd/tenacity](https://github.com/jd/tenacity) |

The container build also uses the [uv container image](https://github.com/astral-sh/uv-docker-example) and the [Python official image](https://github.com/docker-library/python). The resulting image retains installed package metadata and license files.

This notice is informational and does not replace the license terms distributed with each component. Review the complete locked dependency set and container base-image notices before redistributing a built image.
