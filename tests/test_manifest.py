import json
import zipfile
from pathlib import Path

import pytest

from agent_app.manifest import ManifestValues, package_app, render_manifest, validate_manifest


def values() -> ManifestValues:
    return ManifestValues(
        teams_app_id="11111111-1111-1111-1111-111111111111",
        bot_id="22222222-2222-2222-2222-222222222222",
        hostname="agent.example.invalid",
    )


def test_rendered_manifest_targets_personal_teams_and_copilot_only() -> None:
    template = json.loads(Path("appPackage/manifest.template.json").read_text())

    manifest = render_manifest(template, values())

    assert manifest["manifestVersion"] == "1.29"
    assert manifest["version"] == "1.1.0"
    assert manifest["developer"]["name"] == "Community Sample Contributors"
    assert manifest["id"] == values().teams_app_id
    assert manifest["bots"][0]["scopes"] == ["personal", "copilot"]
    assert manifest["bots"][0]["botId"] == values().bot_id
    assert manifest["bots"][0]["supportsFiles"] is True
    assert manifest["copilotAgents"]["customEngineAgents"][0]["id"] == values().bot_id
    assert manifest["webApplicationInfo"] == {
        "id": values().bot_id,
        "resource": f"api://botid-{values().bot_id}",
    }
    assert "*.botframework.com" in manifest["validDomains"]
    validate_manifest(manifest)


def test_manifest_exposes_safe_demo_commands_in_both_scopes() -> None:
    template = json.loads(Path("appPackage/manifest.template.json").read_text())

    manifest = render_manifest(template, values())

    command_list = manifest["bots"][0]["commandLists"][0]
    assert command_list["scopes"] == ["personal", "copilot"]
    assert [command["title"] for command in command_list["commands"]] == [
        "/approval-demo",
        "/image-demo",
        "/whoami",
    ]


def test_manifest_validation_rejects_group_and_team_scopes() -> None:
    template = json.loads(Path("appPackage/manifest.template.json").read_text())
    manifest = render_manifest(template, values())
    manifest["bots"][0]["scopes"].append("groupChat")

    with pytest.raises(ValueError, match="personal and copilot"):
        validate_manifest(manifest)


def test_package_contains_only_manifest_and_two_icons(tmp_path: Path) -> None:
    output = tmp_path / "app.zip"

    digest = package_app(
        template_path=Path("appPackage/manifest.template.json"),
        color_icon=Path("appPackage/color.png"),
        outline_icon=Path("appPackage/outline.png"),
        output_path=output,
        values=values(),
    )

    assert len(digest) == 64
    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == ["color.png", "manifest.json", "outline.png"]
        manifest = json.loads(archive.read("manifest.json"))
    assert manifest["bots"][0]["botId"] == values().bot_id
