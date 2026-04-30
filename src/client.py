"""Thin QBO REST client. Handles auth, refresh, query, get, and sparse update."""
from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

from .auth import get_valid_access_token

load_dotenv()

PROD_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"


class QBOError(RuntimeError):
    pass


class QBOClient:
    def __init__(self, minor_version: int = 73):
        self.minor_version = minor_version
        env = os.environ.get("QBO_ENV", "production").lower()
        self.base_url = SANDBOX_BASE if env == "sandbox" else PROD_BASE
        self._lookup_cache: dict[tuple[str, str], dict] = {}

    # -- core http -----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        access_token, _ = get_valid_access_token()
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _realm(self) -> str:
        _, realm_id = get_valid_access_token()
        return realm_id

    def _url(self, path: str) -> str:
        return f"{self.base_url}/v3/company/{self._realm()}{path}"

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        params = kwargs.pop("params", {}) or {}
        params.setdefault("minorversion", self.minor_version)
        resp = requests.request(
            method,
            self._url(path),
            headers=self._headers(),
            params=params,
            timeout=60,
            **kwargs,
        )
        if not resp.ok:
            raise QBOError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    # -- queries -------------------------------------------------------------

    def query(self, sql: str) -> list[dict]:
        """Run a QBO SQL query. Returns the list of entities (any type)."""
        data = self._request("GET", "/query", params={"query": sql})
        qr = data.get("QueryResponse", {})
        # QBO returns the entity list under the entity-name key, e.g. "Account": [...]
        for k, v in qr.items():
            if isinstance(v, list):
                return v
        return []

    def lookup_ref(self, entity_type: str, name: str) -> dict:
        """Resolve a name to {value, name} ref dict. Cached. entity_type is QBO's name."""
        key = (entity_type, name.strip())
        if key in self._lookup_cache:
            return self._lookup_cache[key]
        # Escape single quotes for QBO SQL
        escaped = name.replace("'", "\\'")
        rows = self.query(f"SELECT Id, Name FROM {entity_type} WHERE Name = '{escaped}'")
        if not rows:
            # Some entities use DisplayName instead of Name
            if entity_type in ("Customer", "Vendor", "Employee"):
                rows = self.query(
                    f"SELECT Id, DisplayName FROM {entity_type} WHERE DisplayName = '{escaped}'"
                )
        if not rows:
            raise QBOError(f"No {entity_type} found with name '{name}'")
        if len(rows) > 1:
            raise QBOError(f"Ambiguous {entity_type} name '{name}': {len(rows)} matches")
        row = rows[0]
        ref = {"value": row["Id"], "name": row.get("Name") or row.get("DisplayName")}
        self._lookup_cache[key] = ref
        return ref

    # -- entities ------------------------------------------------------------

    def get_entity(self, entity_type: str, entity_id: str) -> dict:
        data = self._request("GET", f"/{entity_type.lower()}/{entity_id}")
        return data[entity_type]

    def update_entity(self, entity_type: str, body: dict) -> dict:
        """Sparse update: body must include Id and SyncToken plus the changed fields."""
        body = {**body, "sparse": True}
        data = self._request(
            "POST",
            f"/{entity_type.lower()}",
            json=body,
            params={"operation": "update"},
        )
        return data[entity_type]
