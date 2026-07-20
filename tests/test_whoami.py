from dataclasses import dataclass

from agent_app.teams_app import handle_whoami


@dataclass
class User:
    display_name: str | None


class MeEndpoint:
    def __init__(self, user: User, error: Exception | None = None):
        self.user = user
        self.error = error
        self.calls = 0

    async def get(self) -> User:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.user


class GraphClient:
    def __init__(self, endpoint: MeEndpoint):
        self.me = endpoint


class WhoAmIContext:
    def __init__(self, *, signed_in: bool):
        self.is_signed_in = signed_in
        self.endpoint = MeEndpoint(User("Ada Lovelace"))
        self.user_graph = GraphClient(self.endpoint)
        self.sign_in_calls = 0
        self.sent: list[str] = []

    async def sign_in(self) -> None:
        self.sign_in_calls += 1

    async def send(self, text: str) -> None:
        self.sent.append(text)


async def test_whoami_starts_sso_before_calling_graph() -> None:
    context = WhoAmIContext(signed_in=False)

    await handle_whoami(context)

    assert context.sign_in_calls == 1
    assert context.endpoint.calls == 0
    assert context.sent == []


async def test_whoami_uses_delegated_graph_profile_after_signin() -> None:
    context = WhoAmIContext(signed_in=True)

    await handle_whoami(context)

    assert context.endpoint.calls == 1
    assert context.sent == ["You are signed in as Ada Lovelace."]


async def test_whoami_hides_graph_and_token_error_details() -> None:
    context = WhoAmIContext(signed_in=True)
    context.endpoint.error = RuntimeError("token secret detail")

    await handle_whoami(context)

    assert context.endpoint.calls == 1
    assert context.sent == ["Sign-in or Microsoft Graph request could not be completed."]
    assert "secret detail" not in str(context.sent)
