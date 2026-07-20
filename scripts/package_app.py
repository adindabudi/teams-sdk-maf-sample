import argparse
from pathlib import Path

from agent_app.manifest import ManifestValues, package_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Microsoft 365 app package")
    parser.add_argument("--teams-app-id", required=True)
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--out", type=Path, default=Path("dist/teams-sdk-maf-sample.zip"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    digest = package_app(
        template_path=Path("appPackage/manifest.template.json"),
        color_icon=Path("appPackage/color.png"),
        outline_icon=Path("appPackage/outline.png"),
        output_path=args.out,
        values=ManifestValues(
            teams_app_id=args.teams_app_id,
            bot_id=args.bot_id,
            hostname=args.hostname,
        ),
    )
    print(f"zip={args.out}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
