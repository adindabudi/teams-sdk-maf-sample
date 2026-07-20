# pyright: reportMissingTypeStubs=false

from microsoft_teams.cards import AdaptiveCard, Image, TextBlock


def build_image_card(
    *,
    image_url: str,
    alt_text: str,
    ttl_minutes: int,
) -> AdaptiveCard:
    return AdaptiveCard(
        version="1.5",
        body=[
            TextBlock(
                text="Generated deployment snapshot",
                weight="Bolder",
                size="Large",
                wrap=True,
            ),
            Image(
                url=image_url,
                alt_text=alt_text,
                size="Stretch",
            ),
            TextBlock(
                text=f"Available for {ttl_minutes} minutes.",
                is_subtle=True,
                wrap=True,
            ),
        ],
    )
