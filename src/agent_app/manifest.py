import hashlib
import json
import struct
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class ManifestValues:
    teams_app_id: str
    bot_id: str
    hostname: str


def render_manifest(template: dict[str, Any], values: ManifestValues) -> dict[str, Any]:
    manifest = deepcopy(template)
    manifest["id"] = values.teams_app_id
    manifest["developer"]["websiteUrl"] = f"https://{values.hostname}"
    manifest["developer"]["privacyUrl"] = f"https://{values.hostname}/privacy"
    manifest["developer"]["termsOfUseUrl"] = f"https://{values.hostname}/terms"
    manifest["bots"][0]["botId"] = values.bot_id
    manifest["copilotAgents"]["customEngineAgents"][0]["id"] = values.bot_id
    manifest["validDomains"] = ["*.botframework.com", values.hostname]
    manifest["webApplicationInfo"] = {
        "id": values.bot_id,
        "resource": f"api://botid-{values.bot_id}",
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("manifestVersion") != "1.29":
        raise ValueError("manifestVersion must be 1.29")
    bots_value: object = manifest.get("bots")
    if not isinstance(bots_value, list):
        raise ValueError("manifest must contain exactly one bot")
    bots = cast(list[object], bots_value)
    if len(bots) != 1:
        raise ValueError("manifest must contain exactly one bot")
    bot_value = bots[0]
    if not isinstance(bot_value, dict):
        raise ValueError("manifest bot must be an object")
    bot = cast(dict[str, object], bot_value)
    if bot.get("scopes") != ["personal", "copilot"]:
        raise ValueError("bot scopes must contain exactly personal and copilot")
    bot_id = bot.get("botId")
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("botId is required")

    copilot_value: object = manifest.get("copilotAgents")
    if not isinstance(copilot_value, dict):
        raise ValueError("copilotAgents is required")
    copilot = cast(dict[str, object], copilot_value)
    engines_value = copilot.get("customEngineAgents")
    if not isinstance(engines_value, list):
        raise ValueError("manifest must contain exactly one custom engine agent")
    engines = cast(list[object], engines_value)
    if len(engines) != 1:
        raise ValueError("manifest must contain exactly one custom engine agent")
    engine_value = engines[0]
    if not isinstance(engine_value, dict):
        raise ValueError("custom engine agent must be an object")
    custom_engine_id = cast(dict[str, object], engine_value).get("id")
    if custom_engine_id != bot_id:
        raise ValueError("custom engine ID must match botId")

    web_info = manifest.get("webApplicationInfo")
    if web_info != {"id": bot_id, "resource": f"api://botid-{bot_id}"}:
        raise ValueError("webApplicationInfo must match bot SSO identity")
    valid_domains = manifest.get("validDomains")
    if not isinstance(valid_domains, list) or "*.botframework.com" not in valid_domains:
        raise ValueError("Bot Framework OAuth domain is required")
    if "__" in json.dumps(manifest):
        raise ValueError("manifest contains unresolved template tokens")


def package_app(
    *,
    template_path: Path,
    color_icon: Path,
    outline_icon: Path,
    output_path: Path,
    values: ManifestValues,
) -> str:
    template = json.loads(template_path.read_text(encoding="utf-8"))
    manifest = render_manifest(template, values)
    _validate_png(color_icon, expected_size=(192, 192))
    _validate_png(outline_icon, expected_size=(32, 32))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    files = {
        "manifest.json": (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
        "color.png": color_icon.read_bytes(),
        "outline.png": outline_icon.read_bytes(),
    }
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)

    return hashlib.sha256(output_path.read_bytes()).hexdigest()


def _validate_png(path: Path, *, expected_size: tuple[int, int]) -> None:
    content = path.read_bytes()
    if not content.startswith(b"\x89PNG\r\n\x1a\n") or len(content) < 24:
        raise ValueError(f"{path.name} is not a valid PNG")
    size = struct.unpack(">II", content[16:24])
    if size != expected_size:
        raise ValueError(f"{path.name} must be {expected_size[0]}x{expected_size[1]}")
