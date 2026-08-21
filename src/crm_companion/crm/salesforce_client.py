"""Thin async Salesforce REST client.

Deliberately narrow: only the operations the tool layer needs, with the two
behaviours that matter in practice - transparent re-auth when a session expires
mid-conversation, and errors that carry Salesforce's own error code rather than a
bare HTTP status, because ``400`` alone never explains a Salesforce rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import httpx

from crm_companion.crm.salesforce_auth import TokenProvider

__all__ = [
    "SalesforceClient",
    "SalesforceError",
    "UpsertResult",
]

_MAX_PAGES = 20


class SalesforceError(RuntimeError):
    def __init__(self, status_code: int, error_code: str, message: str, *, fields: tuple = ()):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.fields = fields
        detail = f"[{error_code}] {message}" if error_code else message
        if fields:
            detail += f" (fields: {', '.join(fields)})"
        super().__init__(f"HTTP {status_code}: {detail}")

    @classmethod
    def from_response(cls, response: httpx.Response) -> SalesforceError:
        try:
            body = response.json()
        except ValueError:
            return cls(response.status_code, "", response.text.strip()[:300])

        if isinstance(body, list) and body:
            first = body[0]
            return cls(
                response.status_code,
                first.get("errorCode", ""),
                first.get("message", ""),
                fields=tuple(first.get("fields") or ()),
            )
        if isinstance(body, dict):
            return cls(
                response.status_code,
                body.get("errorCode", "") or body.get("error", ""),
                body.get("message", "") or body.get("error_description", ""),
            )
        return cls(response.status_code, "", str(body)[:300])


@dataclass(frozen=True)
class UpsertResult:
    """``created`` distinguishes a genuine create from an idempotent replay."""

    id: str
    created: bool


class SalesforceClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        api_version: str = "v62.0",
        timeout: float = 15.0,
    ) -> None:
        self._tokens = token_provider
        self._api_version = api_version
        self._http = httpx.AsyncClient(timeout=timeout)
        self._describe_cache: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def data_path(self) -> str:
        return f"/services/data/{self._api_version}"

    # ---- reads -------------------------------------------------------------

    async def query(self, soql: str, *, max_records: int | None = None) -> list[dict[str, Any]]:
        """Run SOQL, following pagination until exhausted or ``max_records`` reached."""
        payload = await self._request("GET", f"{self.data_path}/query", params={"q": soql})
        records: list[dict[str, Any]] = list(payload.get("records", []))

        pages = 0
        while not payload.get("done", True) and payload.get("nextRecordsUrl"):
            if max_records is not None and len(records) >= max_records:
                break
            pages += 1
            if pages >= _MAX_PAGES:
                break
            payload = await self._request("GET", payload["nextRecordsUrl"])
            records.extend(payload.get("records", []))

        return records[:max_records] if max_records is not None else records

    async def query_one(self, soql: str) -> dict[str, Any] | None:
        records = await self.query(soql, max_records=1)
        return records[0] if records else None

    async def count(self, soql: str) -> int:
        """Run an aggregate COUNT() query and return the scalar."""
        records = await self.query(soql)
        if not records:
            return 0
        row = records[0]
        for key in ("expr0", "cnt", "total"):
            if key in row:
                return int(row[key] or 0)
        numeric = [v for k, v in row.items() if k != "attributes" and isinstance(v, int)]
        return int(numeric[0]) if numeric else 0

    async def describe(self, sobject: str) -> dict[str, Any]:
        """Describe an object, cached for the client's lifetime.

        Picklist values and field metadata change rarely, and stage resolution
        would otherwise describe on every single turn of a conversation.
        """
        if sobject not in self._describe_cache:
            self._describe_cache[sobject] = await self._request(
                "GET", f"{self.data_path}/sobjects/{sobject}/describe"
            )
        return self._describe_cache[sobject]

    async def picklist_values(self, sobject: str, field: str) -> tuple[str, ...]:
        described = await self.describe(sobject)
        for candidate in described.get("fields", []):
            if candidate.get("name") == field:
                return tuple(
                    value["value"]
                    for value in candidate.get("picklistValues", [])
                    if value.get("active", True)
                )
        return ()

    # ---- writes ------------------------------------------------------------

    async def create(self, sobject: str, data: dict[str, Any]) -> str:
        payload = await self._request("POST", f"{self.data_path}/sobjects/{sobject}", json=data)
        return payload["id"]

    async def update(self, sobject: str, record_id: str, data: dict[str, Any]) -> None:
        await self._request("PATCH", f"{self.data_path}/sobjects/{sobject}/{record_id}", json=data)

    async def upsert_by_external_id(
        self, sobject: str, external_field: str, key: str, data: dict[str, Any]
    ) -> UpsertResult:
        """Upsert on an External ID field.

        The response body carries ``created``, so a replayed call is detectable
        without inspecting status codes.
        """
        payload = await self._request(
            "PATCH",
            f"{self.data_path}/sobjects/{sobject}/{external_field}/{key}",
            json=data,
        )
        return UpsertResult(id=payload["id"], created=bool(payload.get("created", False)))

    async def delete(self, sobject: str, record_id: str) -> None:
        await self._request("DELETE", f"{self.data_path}/sobjects/{sobject}/{record_id}")

    async def post_feed_item(self, *, subject_id: str, segments: list[dict[str, Any]]) -> str:
        """Post to Chatter with structured message segments.

        Segments rather than a plain string is what makes a mention an actual
        notification instead of text that merely looks like one.
        """
        payload = await self._request(
            "POST",
            f"{self.data_path}/chatter/feed-elements",
            json={
                "feedElementType": "FeedItem",
                "subjectId": subject_id,
                "body": {"messageSegments": segments},
            },
        )
        return payload["id"]

    # ---- plumbing ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._send(method, path, params=params, json=json, refresh=False)

        # A session can expire mid-conversation; re-auth once and retry rather
        # than surfacing an auth error to someone driving.
        if response.status_code == 401:
            response = await self._send(method, path, params=params, json=json, refresh=True)

        if response.status_code >= 400:
            raise SalesforceError.from_response(response)

        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        refresh: bool,
    ) -> httpx.Response:
        credentials = await self._tokens.refresh() if refresh else await self._tokens.get()
        url = path if path.startswith("http") else f"{credentials.instance_url}{path}"
        return await self._http.request(
            method,
            url,
            params=params,
            json=json,
            headers={**credentials.auth_header(), "Content-Type": "application/json"},
        )
