"""Read public PRs anonymously and connected repositories with scoped GitHub App access."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlsplit

import httpx

from database import Database, FinishedReview, Task
from github_app import GitHubConnectionError, get_github_app, normalize_repository
from review_urls import parse_review_url

logger = logging.getLogger(__name__)
CACHE_TTL = timedelta(minutes=5)
FETCH_TIMEOUT = 8
FETCH_CONCURRENCY = 4
# Enrichment is optional: large queues must still respond promptly.
BATCH_TIMEOUT = 8


class ReviewMetadataClient:
    """Never use a deployment-wide repository token on behalf of unrelated teams."""

    def request_for(self, url: str) -> tuple[str, dict[str, str]] | None:
        parsed_review = parse_review_url(url)
        if parsed_review is None:
            return None
        parsed = urlsplit(url)
        if parsed.hostname.lower() != "github.com":
            return None
        _, (_, project, number) = parsed_review
        segments = [unquote(part) for part in project.strip("/").split("/")]
        if any(part in (".", "..") or "/" in part or "\\" in part for part in segments):
            return None
        project_path = "/".join(quote(part, safe="") for part in segments)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "tg-work-queue"}
        return f"https://api.github.com/repos/{project_path}/pulls/{number}", headers

    async def fetch(self, http: httpx.AsyncClient, url: str, *, token: str | None = None,
                    repository_id: int | None = None) -> tuple[str, str] | None:
        request = self.request_for(url)
        if request is None:
            return None
        endpoint, headers = request
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            # The outer timeout also bounds a server that keeps trickling response bytes.
            response = await asyncio.wait_for(http.get(endpoint, headers=headers, follow_redirects=False), FETCH_TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("title"), str):
                raise ValueError("Missing title")
            if repository_id is not None:
                base = data.get("base")
                repository = base.get("repo") if isinstance(base, dict) else None
                if not isinstance(repository, dict) or repository.get("id") != repository_id:
                    raise ValueError("Repository identity changed")
            description = data.get("body") or ""
            if not isinstance(description, str):
                raise ValueError("Invalid description")
            return data["title"], description
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            # Don't log response bodies, credentials, or exception URLs.
            logger.warning("Review metadata lookup failed for %s (%s)", urlsplit(endpoint).hostname,
                           type(error).__name__)
            return None


async def refresh_review_metadata(db: Database, reviews: list[Task | FinishedReview],
                                  client: ReviewMetadataClient | None = None) -> None:
    """Public data is cached per chat. Connected data is reauthorized and never saved to disk."""
    if not reviews:
        return
    client = client or ReviewMetadataClient()
    private_results = {}
    connections = {}
    for review in reviews:
        parsed = parse_review_url(review.url)
        if parsed and urlsplit(review.url).hostname.lower() == "github.com":
            repository = normalize_repository(unquote(parsed[1][1]).strip("/"))
            connection = db.get_github_connection(review.chat_id, repository) if repository else None
            if connection:
                connections[(review.chat_id, review.url)] = connection
                # Never fall back to old metadata when GitHub access can no longer be verified.
                review.title = review.description = None
    app = get_github_app() if connections else None
    token_tasks = {}
    async with httpx.AsyncClient(timeout=5, follow_redirects=False) as http:
        async def refresh(chat_id, url):
            connection = connections.get((chat_id, url))
            if connection:
                if app is None or client.request_for(url) is None:
                    return
                try:
                    token_key = connection["connection_id"]
                    if token_key not in token_tasks:
                        token_tasks[token_key] = asyncio.create_task(app.installation_token(http, connection))
                    token = await token_tasks[token_key]
                    private_results[(chat_id, url)] = await client.fetch(
                        http, url, token=token, repository_id=connection["repository_id"])
                except GitHubConnectionError:
                    logger.warning("GitHub App access unavailable for chat %s", chat_id)
                return
            cached = db.get_review_metadata(chat_id, url)
            now = datetime.now(timezone.utc)
            if cached and now - datetime.fromisoformat(cached["checked_at"]) < CACHE_TTL:
                return
            if client.request_for(url) is None:
                return
            details = await client.fetch(http, url)
            db.save_review_metadata(chat_id, url, *(details or (None, None)))

        keys = iter(dict.fromkeys((review.chat_id, review.url) for review in reviews))

        async def worker():
            for chat_id, url in keys:
                await refresh(chat_id, url)
                # Cached entries do synchronous work; give the deadline and other chats a turn.
                await asyncio.sleep(0)

        workers = [asyncio.create_task(worker()) for _ in range(min(FETCH_CONCURRENCY, len(reviews)))]
        try:
            async with asyncio.timeout(BATCH_TIMEOUT):
                await asyncio.gather(*workers)
        except TimeoutError:
            logger.info("Review metadata deadline reached; returning available details")
        finally:
            # Also clean up shared token requests if a caller cancels or a worker fails.
            pending = [*workers, *token_tasks.values()]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    for review in reviews:
        connection = connections.get((review.chat_id, review.url))
        if connection:
            current = db.get_github_connection(review.chat_id, connection["repository"])
            details = private_results.get((review.chat_id, review.url))
            if current and current["connection_id"] == connection["connection_id"] and details:
                review.title, review.description = details
            continue
        cached = db.get_review_metadata(review.chat_id, review.url)
        if cached:
            review.title = cached["title"]
            review.description = cached["description"]
