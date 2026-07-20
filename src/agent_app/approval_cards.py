# pyright: reportMissingTypeStubs=false

from microsoft_teams.cards import (
    AdaptiveCard,
    ExecuteAction,
    SubmitData,
    TextBlock,
)

from agent_app.approvals import ApprovalRecord


def build_approval_card(record: ApprovalRecord) -> AdaptiveCard:
    return AdaptiveCard(
        version="1.5",
        body=[
            TextBlock(
                text="Human approval required",
                weight="Bolder",
                size="Large",
                wrap=True,
            ),
            TextBlock(text=record.action_summary, wrap=True),
            TextBlock(
                text="This request expires in five minutes.",
                is_subtle=True,
                wrap=True,
            ),
        ],
        actions=[
            ExecuteAction(title="Approve", style="positive")
            .with_data(
                SubmitData(
                    "approval_response",
                    {
                        "approval_id": record.approval_id,
                        "decision": "approved",
                    },
                )
            )
            .with_associated_inputs("none"),
            ExecuteAction(title="Reject", style="destructive")
            .with_data(
                SubmitData(
                    "approval_response",
                    {
                        "approval_id": record.approval_id,
                        "decision": "rejected",
                    },
                )
            )
            .with_associated_inputs("none"),
        ],
    )


def build_decision_card(label: str, *, approved: bool) -> AdaptiveCard:
    return AdaptiveCard(
        version="1.5",
        body=[
            TextBlock(
                text=label,
                weight="Bolder",
                color="Good" if approved else "Attention",
                wrap=True,
            )
        ],
    )
