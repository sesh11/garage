"""Devin v3 API client."""

import httpx

from garage.agents.base import SessionState, SessionStatus, CreatedSession


class DevinClient:
    """Create and inspect sessions through Devin's v3 API."""

    def __init__(
        self,
        api_key: str,
        org_id: str,
        *,
        base_url: str = "https://api.devin.ai",
        max_acu: int = 5,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        """Configure a Devin API client."""
        self.api_key = api_key
        self.org_id = org_id
        self.base_url = base_url.rstrip("/")
        self.max_acu = max_acu
        self._http = http

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        if self._http is not None:
            return await self._http.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )
        async with httpx.AsyncClient() as client:
            return await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )

    def _sessions_path(self) -> str:
        return f"/v3/organizations/{self.org_id}/sessions"

    async def create_session(
        self,
        prompt: str,
        *,
        title: str,
        tags: list[str],
        structured_output_schema: dict[str, object],
        repos: list[str] | None = None,
        platform: str | None = None,
    ) -> CreatedSession:
        """Start a Devin session; raises on HTTP errors. ``platform`` names an outpost pool, default cloud."""
        payload: dict[str, object] = {
            "prompt": prompt,
            "title": title,
            "tags": tags,
            "max_acu_limit": self.max_acu,
            "structured_output_schema": structured_output_schema,
        }
        if repos:
            payload["repos"] = repos
        if platform:
            payload["platform"] = platform
        response = await self._request("POST", self._sessions_path(), payload)
        response.raise_for_status()
        return CreatedSession.model_validate(response.json())

    async def get_session(self, session_id: str) -> SessionState:
        """Fetch a session's current state; raises on HTTP errors."""
        response = await self._request("GET", f"{self._sessions_path()}/{session_id}")
        response.raise_for_status()
        return SessionState.model_validate(response.json())

    @staticmethod
    def map_status(status: str, status_detail: str) -> SessionStatus | None:
        """Translate a Devin session status into Garage's normalized status."""
        if status in {"new", "claimed", "running", "resuming"}:
            if status_detail in {"waiting_for_user", "waiting_for_approval"}:
                return SessionStatus.blocked
            if status_detail == "finished":
                return SessionStatus.finished
            return SessionStatus.working
        if status == "exit":
            return SessionStatus.finished
        if status == "error":
            return SessionStatus.failed
        if status == "suspended":
            if status_detail == "finished":
                return SessionStatus.finished
            if status_detail in {"inactivity", "user_request"}:
                return SessionStatus.expired
            return SessionStatus.failed
        return None

    @staticmethod
    def error_detail(error: Exception) -> str:
        """Short, log-safe description of a failed Devin API call."""
        if isinstance(error, httpx.HTTPStatusError):
            response = error.response
            return f"HTTP {response.status_code}: {response.text[:500]}"
        return str(error)
