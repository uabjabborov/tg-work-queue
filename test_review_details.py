import os
import unittest
from unittest.mock import patch

import httpx

from review_details import ReviewDetails, clean_description, fetch_review_details


class ReviewDetailsTests(unittest.IsolatedAsyncioTestCase):
    def client(self, status=200, payload=None):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            return httpx.Response(status, json=payload if payload is not None else {})

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_github_uses_pull_api_and_optional_token(self):
        payload = {"title": " Fix login ", "body": "<!-- template -->\r\nRedirect after login.\n\n\n\nDone."}
        with patch.dict(os.environ, {"GITHUB_TOKEN": "gh-secret", "GITLAB_TOKEN": "gl-secret"}):
            async with self.client(payload=payload) as client:
                details = await fetch_review_details(client, "https://github.com/Owner/Repo/pull/0120/?tab=files")

        self.assertEqual(details, ReviewDetails("Fix login", "Redirect after login.\n\nDone."))
        request, = self.requests
        self.assertEqual(str(request.url), "https://api.github.com/repos/Owner/Repo/pulls/120")
        self.assertEqual(request.headers["Authorization"], "Bearer gh-secret")
        self.assertNotIn("PRIVATE-TOKEN", request.headers)

    async def test_gitlab_uses_configured_host_encoded_project_and_token(self):
        payload = {"title": "Add cache", "description": None}
        env = {"GITLAB_URL": "https://GitLab.example.com/", "GITLAB_TOKEN": "gl-secret", "GITHUB_TOKEN": "gh-secret"}
        with patch.dict(os.environ, env):
            async with self.client(payload=payload) as client:
                details = await fetch_review_details(
                    client, "http://gitlab.example.com/team/sub/repo/-/merge_requests/7#note_1"
                )

        self.assertEqual(details, ReviewDetails("Add cache", ""))
        request, = self.requests
        self.assertEqual(
            str(request.url),
            "https://gitlab.example.com/api/v4/projects/team%2Fsub%2Frepo/merge_requests/7",
        )
        self.assertEqual(request.headers["PRIVATE-TOKEN"], "gl-secret")
        self.assertNotIn("Authorization", request.headers)

    async def test_hosts_other_than_github_and_gitlab_url_are_never_contacted(self):
        with patch.dict(os.environ, {"GITLAB_URL": "https://gitlab.example.com", "GITLAB_TOKEN": "gl-secret"}):
            async with self.client(payload={"title": "x"}) as client:
                for url in (
                    "https://evil.example.com/team/repo/-/merge_requests/1",
                    "https://gitlab.example.com:8443/team/repo/-/merge_requests/1",
                    "https://github.com/owner/repo/issues/1",
                ):
                    with self.subTest(url=url):
                        self.assertIsNone(await fetch_review_details(client, url))
        self.assertEqual(self.requests, [])

    async def test_failures_return_none(self):
        for status, payload in ((404, {"message": "Not Found"}), (200, {"title": ""}), (200, ["not", "a", "dict"])):
            with self.subTest(status=status, payload=payload):
                async with self.client(status, payload) as client:
                    self.assertIsNone(await fetch_review_details(client, "https://github.com/o/r/pull/1"))

        def timeout(request):
            raise httpx.ConnectTimeout("slow", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            self.assertIsNone(await fetch_review_details(client, "https://github.com/o/r/pull/1"))

    def test_description_is_capped(self):
        self.assertEqual(len(clean_description("x" * 5000)), 1000)
        self.assertEqual(clean_description(None), "")


if __name__ == "__main__":
    unittest.main()
