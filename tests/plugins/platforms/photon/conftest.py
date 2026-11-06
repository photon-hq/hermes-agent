"""Photon 2.0 API served in-process for the setup tests.

The ``photon_api`` fixture patches the real ``photon_api.Photon`` client the plugin builds so
its requests go through an ``httpx.MockTransport``: the SDK still builds and validates every
request and response, only the network is replaced. The reply builders satisfy the SDK's
response models, so schema drift in a new SDK pin fails here rather than in production.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

import httpx
import pytest

from plugins.platforms.photon import auth as photon_auth

_PHOTON_ENV = (
    "PHOTON_PROJECT_ID", "PHOTON_API_KEY", "PHOTON_PROJECT_SECRET", "PHOTON_API_HOST",
    "PHOTON_ALLOWED_USERS", "PHOTON_HOME_CHANNEL",
)

Reply = dict | tuple | Callable[[httpx.Request], httpx.Response]


class FakePhotonApi:
    """Routes ``(method, path)`` to queued replies: a JSON body (200), a ``(status, body)``
    tuple, or a callable taking the request. The last reply of a queue repeats."""

    NOW = "2026-11-01T00:00:00.000Z"
    ORG_ID = "pho_org_00000000000000000000000001"
    PROJECT_ID = "pho_prj_00000000000000000000000001"

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Reply]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.routes[(method, path)] = list(replies)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            status, body = self.problem(404, "NOT_FOUND", f"no fake route for {request.method} {request.url.path}")
            return httpx.Response(status, json=body)
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(reply):
            return reply(request)
        status, body = reply if isinstance(reply, tuple) else (200, reply)
        return httpx.Response(status, json=body)

    @staticmethod
    def body(request: httpx.Request) -> Any:
        return json.loads(request.content or b"null")

    @classmethod
    def problem(cls, status: int, code: str, detail: str = "refused") -> tuple:
        return status, {"type": "about:blank", "title": code, "status": status, "code": code, "detail": detail}

    @classmethod
    def token_grant(cls, access: str = "acc-1", refresh: str = "ref-1", expires_in: float = 3600) -> dict:
        return {"access_token": access, "expires_in": expires_in, "refresh_token": refresh,
                "user": {"email": "ops@example.com", "first_name": None, "id": "pho_acc_1", "last_name": None}}

    @classmethod
    def organization(cls, org_id: str = ORG_ID, name: str = "Acme") -> dict:
        return {"avatarUrl": None, "createdAt": cls.NOW, "name": name, "organizationId": org_id,
                "updatedAt": cls.NOW, "version": "1"}

    @classmethod
    def project(cls, project_id: str = PROJECT_ID, name: str = "Hermes Agent") -> dict:
        return {"agentProfile": {"avatarUrl": "https://example.invalid/a.png", "firstName": "Hermes", "lastName": None},
                "createdAt": cls.NOW, "createdByAccountId": "pho_acc_1", "createdByActorId": "pho_acc_1",
                "createdByActorKind": "account", "deletedAt": None, "name": name, "organizationId": cls.ORG_ID,
                "projectId": project_id, "slug": "hermes-agent-0000", "updatedAt": cls.NOW}

    @classmethod
    def api_key(cls, api_key_id: str = "pho_key_new") -> dict:
        return {"apiKeyId": api_key_id, "createdAt": cls.NOW, "createdByAccountId": "pho_acc_1", "expiresAt": None,
                "name": "Hermes Agent (host)", "permissions": list(photon_auth.PROJECT_KEY_PERMISSIONS),
                "permissionsUpdatedAt": None, "projectId": cls.PROJECT_ID, "revokedAt": None}

    @classmethod
    def assignment(cls, handle: str, number: str = "+16282679185") -> dict:
        return {"assignedPhoneNumber": number, "assignmentId": "pho_asg_1", "createdAt": cls.NOW,
                "handle": handle, "platform": "imessage", "projectId": cls.PROJECT_ID, "updatedAt": cls.NOW}

    @classmethod
    def shared_mode(cls) -> dict:
        return {"dedicatedLineAvailableNow": False, "maxUserAssignments": 5, "mode": "shared"}

    @classmethod
    def received_text(cls, handle: str) -> dict:
        project_id = cls.PROJECT_ID
        return {"content": {"format": "plain", "text": "hi", "type": "text"}, "createdAt": cls.NOW,
                "id": "pho_msg_1", "metadata": {}, "platform": "imessage", "projectId": project_id,
                "sentBy": {"details": {"type": "shared"}, "handle": handle, "id": "pho_usr_1", "kind": "person",
                           "metadata": {}, "platform": "imessage", "projectId": project_id},
                "space": {"details": {}, "id": "pho_spc_1", "kind": "direct", "metadata": {},
                          "participantIds": ["pho_usr_1", "pho_usr_2"], "platform": "imessage",
                          "projectId": project_id},
                "status": "received", "timestamps": {"receivedAt": cls.NOW}, "updatedAt": cls.NOW}


@pytest.fixture
def photon_home(monkeypatch: pytest.MonkeyPatch):
    """Scrub Photon env around a test (save_env_value writes os.environ directly)."""
    for key in _PHOTON_ENV:
        monkeypatch.delenv(key, raising=False)
    yield
    for key in _PHOTON_ENV:
        os.environ.pop(key, None)


@pytest.fixture
def photon_api(monkeypatch: pytest.MonkeyPatch, photon_home) -> FakePhotonApi:
    api = FakePhotonApi()
    client = httpx.Client(transport=httpx.MockTransport(api.handle))
    real = photon_auth.Photon
    monkeypatch.setattr(photon_auth, "Photon", lambda **kw: real(**kw, client=client))
    yield api
    client.close()
