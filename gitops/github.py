"""Small GitHub API client used only to create review-first pull requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

    @property
    def creates_pull_requests(self) -> bool:
        """Whether this client is configured to call the GitHub API."""
        return self._create_pull_request

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
                if response.status_code == 422:
                    existing = await self._find_existing_pull_request(
                        client,
                        headers=headers,
                        head=head,
                        base=base,
                    )
                    if existing is not None:
                        return PullRequestResult(
                            created=False,
                            prepared=True,
                            url=existing,
                        )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return PullRequestResult(
                created=False,
                prepared=True,
                error=(
                    f"GitHub API returned {exc.response.status_code}: "
                    f"{self._safe_github_error(exc.response)}"
                ),
            )
        except httpx.HTTPError as exc:
            return PullRequestResult(
                created=False,
                prepared=True,
                error=self._redact(str(exc)),
            )

        data = response.json()
        return PullRequestResult(created=True, prepared=True, url=data.get("html_url"))

    async def _find_existing_pull_request(
        self,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str],
        head: str,
        base: str,
    ) -> str | None:
        """Return an existing open PR URL for the same branch, if GitHub has one."""
        assert self._repository is not None
        owner = self._repository.split("/", 1)[0]
        response = await client.get(
            f"/repos/{self._repository}/pulls",
            headers=headers,
            params={
                "state": "open",
                "head": f"{owner}:{head}",
                "base": base,
            },
        )
        response.raise_for_status()
        pulls = response.json()
        if not isinstance(pulls, list) or not pulls:
            return None
        first: Any = pulls[0]
        if isinstance(first, dict):
            url = first.get("html_url")
            if isinstance(url, str):
                return url
        return None

    def _safe_github_error(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return self._redact(response.text[:300])
        if isinstance(payload, dict):
            message = payload.get("message")
            if isinstance(message, str):
                return self._redact(message)
        return "unexpected GitHub error response"

    def _redact(self, message: str) -> str:
        if self._token is None:
            return message
        return message.replace(self._token.get_secret_value(), "[redacted]")
