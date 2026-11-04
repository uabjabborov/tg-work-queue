"""GitHub App device authorization and repository-scoped installation access.

Only non-secret connection IDs are persisted. Device and access tokens stay in memory;
the app signing key is supplied by the operator as a mounted PEM file.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt

API = "https://api.github.com"
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+")
logger = logging.getLogger(__name__)


def normalize_repository(value: str) -> str | None:
    if not REPOSITORY_PATTERN.fullmatch(value) or value.split("/")[1] in (".", ".."):
        return None
    return value.lower()


class GitHubConnectionError(Exception):
    """A safe, user-facing error that contains no provider payloads or secrets."""


@dataclass
class DeviceLogin:
    repository: str
    client_id: str
    device_code: str = field(repr=False)
    expires_at: float
    interval: int
    next_poll: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class GitHubApp:
    def __init__(self, app_id: str, private_key: str):
        self.app_id = app_id
        self._private_key = private_key
        self.pending: dict[tuple[int, int], DeviceLogin] = {}

    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id},
                          self._private_key, algorithm="RS256")

    async def request(self, http: httpx.AsyncClient, method: str, path: str, token: str, **kwargs) -> dict:
        try:
            response = await asyncio.wait_for(http.request(
                method, API + path, headers={"Authorization": f"Bearer {token}",
                                            "Accept": "application/vnd.github+json",
                                            "X-GitHub-Api-Version": "2022-11-28"},
                follow_redirects=False, **kwargs), 10)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid response")
            return data
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise GitHubConnectionError("GitHub couldn't verify access. Check the App installation and repository permissions, then try again.") from None

    async def oauth(self, http: httpx.AsyncClient, path: str, data: dict) -> dict:
        try:
            response = await asyncio.wait_for(http.post(
                "https://github.com" + path, data=data, headers={"Accept": "application/json"},
                follow_redirects=False), 10)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Invalid response")
            return result
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise GitHubConnectionError("GitHub authorization is unavailable. Please try again later.") from None

    async def start(self, http: httpx.AsyncClient, chat_id: int, user_id: int, repository: str) -> tuple[str, str]:
        repository = normalize_repository(repository)
        if repository is None:
            raise GitHubConnectionError("Use a repository name such as owner/repo.")
        now = time.monotonic()
        self.pending = {key: login for key, login in self.pending.items() if login.expires_at > now}
        if len(self.pending) >= 1000:
            raise GitHubConnectionError("Too many pending connections. Please try again later.")
        app = await self.request(http, "GET", "/app", self.app_jwt())
        if (not isinstance(app.get("client_id"), str) or not isinstance(app.get("slug"), str)
                or not re.fullmatch(r"[a-zA-Z0-9-]+", app["slug"])):
            raise GitHubConnectionError("The GitHub App configuration could not be verified.")
        data = await self.oauth(http, "/login/device/code", {"client_id": app["client_id"], "scope": "offline_access"})
        if (not isinstance(data.get("device_code"), str) or not isinstance(data.get("user_code"), str)
                or not isinstance(data.get("expires_in"), int) or not isinstance(data.get("interval"), int)):
            raise GitHubConnectionError("Device authorization failed. The operator must enable Device Flow in the GitHub App settings.")
        self.pending[(chat_id, user_id)] = DeviceLogin(repository, app["client_id"], data["device_code"],
                                                      time.monotonic() + min(data["expires_in"], 900),
                                                      max(data["interval"], 5))
        return f"https://github.com/apps/{app['slug']}/installations/new", data["user_code"]

    def cancel(self, chat_id: int, user_id: int) -> None:
        self.pending.pop((chat_id, user_id), None)

    def cancel_repository(self, chat_id: int, repository: str) -> None:
        for key, login in list(self.pending.items()):
            if key[0] == chat_id and login.repository == repository:
                self.pending.pop(key, None)

    async def finish(self, http: httpx.AsyncClient, chat_id: int, user_id: int) -> dict:
        key = (chat_id, user_id)
        login = self.pending.get(key)
        if login is None or login.expires_at <= time.monotonic():
            self.pending.pop(key, None)
            raise GitHubConnectionError("No active connection request. Run !wgithub connect owner/repo again.")
        async with login.lock:
            if self.pending.get(key) is not login:
                raise GitHubConnectionError("This connection request is no longer active.")
            if time.monotonic() < login.next_poll:
                raise GitHubConnectionError("Please wait a few seconds before running !wgithub finish again.")
            login.next_poll = time.monotonic() + login.interval
            data = await self.oauth(http, "/login/oauth/access_token", {
                "client_id": login.client_id, "device_code": login.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            })
            error = data.get("error")
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    login.interval += 5
                    login.next_poll = time.monotonic() + login.interval
                raise GitHubConnectionError("Finish authorizing on GitHub, then wait a few seconds and send !wgithub finish again.")
            if error or not isinstance(data.get("access_token"), str):
                self.pending.pop(key, None)
                raise GitHubConnectionError("Authorization expired or was denied. Run !wgithub connect owner/repo again.")
            try:
                connection = await self.verify_repository(http, login.repository, data["access_token"])
                if self.pending.get(key) is not login:
                    raise GitHubConnectionError("This connection request was cancelled or replaced.")
                return connection
            finally:
                # Consume this login on success or failure. Never persist user/refresh tokens.
                if self.pending.get(key) is login:
                    self.pending.pop(key, None)

    async def verify_repository(self, http: httpx.AsyncClient, repository: str, user_token: str) -> dict:
        user = await self.request(http, "GET", "/user", user_token)
        repo = await self.request(http, "GET", f"/repos/{repository}", user_token)
        if (str(repo.get("full_name", "")).lower() != repository
                or not isinstance(repo.get("id"), int) or not isinstance(user.get("id"), int)
                or not isinstance(user.get("login"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", user["login"])):
            raise GitHubConnectionError("GitHub repository and account identities could not be verified.")
        permissions = await self.request(http, "GET",
                                         f"/repos/{repository}/collaborators/{user['login']}/permission", user_token)
        if (permissions.get("permission") != "admin" or not isinstance(permissions.get("user"), dict)
                or permissions["user"].get("id") != user["id"]):
            raise GitHubConnectionError("You must be a GitHub administrator of that exact repository to share it with this chat.")
        installation = await self.request(http, "GET", f"/repos/{repository}/installation", self.app_jwt())
        if not isinstance(installation.get("id"), int) or installation.get("suspended_at"):
            raise GitHubConnectionError("Install the GitHub App on that repository before connecting it.")
        connection = {"repository": repository, "repository_id": repo["id"],
                      "installation_id": installation["id"], "github_user_id": user["id"]}
        await self.installation_token(http, connection)
        return connection

    async def installation_token(self, http: httpx.AsyncClient, connection: dict) -> str:
        data = await self.request(http, "POST",
                                  f"/app/installations/{connection['installation_id']}/access_tokens",
                                  self.app_jwt(), json={"repository_ids": [connection["repository_id"]],
                                                        "permissions": {"pull_requests": "read"}})
        if not isinstance(data.get("token"), str):
            raise GitHubConnectionError("GitHub did not grant repository access.")
        return data["token"]


_app: GitHubApp | None = None
_configuration: tuple[str, str] | None = None


def get_github_app() -> GitHubApp | None:
    global _app, _configuration
    configuration = (os.getenv("GITHUB_APP_ID", ""), os.getenv("GITHUB_APP_PRIVATE_KEY_FILE", ""))
    if configuration == _configuration:
        return _app
    _configuration, _app = configuration, None
    if not all(configuration):
        return None
    try:
        candidate = GitHubApp(configuration[0], Path(configuration[1]).read_text())
        candidate.app_jwt()  # Validate key material before any onboarding or metadata calls.
        _app = candidate
    except (OSError, ValueError, jwt.PyJWTError):
        logger.error("Could not load GitHub App signing configuration")
    return _app
