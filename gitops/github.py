"""Small GitHub API client used only to create review-first pull requests."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic import SecretStr


@dataclass(frozen=True)
class PullRequestResult:
    """Outcome of creating or preparing pull request metadata."""

    created: bool
    prepared: bool
    url: str | None = None
    error: str | None = None


class GitHubClient:
    """Creates pull requests when explicitly configured with repository credentials."""

    def __init__(
        self,
        *,
        repository: str | None,
        api_url: str = "https://api.github.com",
        token: SecretStr | None = None,
        create_pull_request: bool = False,
    ) -> None:
        self._repository = repository
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._create_pull_request = create_pull_request

    async def create_or_prepare_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> PullRequestResult:
        """Create a GitHub PR or safely return prepared metadata when disabled."""
        if not self._create_pull_request:
            return PullRequestResult(created=False, prepared=True)
        if not self._repository:
            return PullRequestResult(
                created=False,
                prepared=True,
                error="GitHub repository is not configured.",
            )
        if self._token is None:
            return PullRequestResult(
                created=False,
                prepared=True,
                error="GitHub token is not configured.",
            )

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = {"title": title, "body": body, "head": head, "base": base}
        try:
            async with httpx.AsyncClient(base_url=self._api_url, timeout=15.0) as client:
                response = await client.post(
                    f"/repos/{self._repository}/pulls",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return PullRequestResult(
                created=False,
                prepared=True,
                error=f"GitHub API returned {exc.response.status_code}.",
            )
        except httpx.HTTPError as exc:
            return PullRequestResult(created=False, prepared=True, error=str(exc))

        data = response.json()
        return PullRequestResult(created=True, prepared=True, url=data.get("html_url"))
