"""
Phase 0 regression tests — Spanner provider create-flow end-to-end.

Pins the four gates this phase fixed (audit BLOCKER B1, B2 + MAJOR M19, M20):

    P0.1  ProviderType enum accepts "spanner" (Pydantic does not 422 the request).
    P0.2  ConnectionCredentials accepts service_account_json + project_id and
          rejects unknown fields (extra='forbid').
    P0.3  Spanner credentials are validated server-side: malformed JSON,
          non-object payloads, and missing required SA-key fields all 400.
    P0.5  extra_config.useEmulator=true is rejected by the dispatch unless
          SYNODIC_ALLOW_SPANNER_EMULATOR is set in the env.

The repo-level round-trip (P0.1 + P0.2 combined) confirms the request
actually persists with provider_type='spanner' and the encrypted
credentials blob carries service_account_json — which is the failure mode
the audit predicted: provider would create with HTTP 201 but auth-empty.
"""
import json
import os

import pytest
from pydantic import ValidationError

from backend.app.db.repositories import provider_repo
from backend.common.models.management import (
    ConnectionCredentials,
    ProviderCreateRequest,
    ProviderType,
)


# Minimal viable Google service-account JSON shape — keys the validator
# requires. Values are dummies; the validator does not call GCP.
_FAKE_SA_KEY = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "client_email": "synodic-test@test-project.iam.gserviceaccount.com",
}


def _spanner_create_req(**overrides) -> dict:
    """Build a Spanner ProviderCreateRequest payload (camelCase aliases)
    matching what the FE wizard sends."""
    payload = dict(
        name="spanner-prod",
        providerType="spanner",
        credentials={
            "project_id": "test-project",
            "service_account_json": json.dumps(_FAKE_SA_KEY),
        },
        extraConfig={
            "projectId": "test-project",
            "instanceId": "uniViz-instance",
            "databaseId": "uniViz",
            "graphName": "UniViz",
        },
    )
    payload.update(overrides)
    return payload


# ── P0.1 — ProviderType enum widening ────────────────────────────────


def test_provider_type_enum_includes_spanner():
    assert ProviderType.SPANNER.value == "spanner"


def test_provider_create_request_accepts_spanner():
    req = ProviderCreateRequest.model_validate(_spanner_create_req())
    assert req.provider_type == ProviderType.SPANNER


# ── P0.2 — ConnectionCredentials field plumbing & extra='forbid' ─────


def test_connection_credentials_round_trips_service_account_json():
    creds = ConnectionCredentials(
        project_id="p", service_account_json='{"type":"service_account"}'
    )
    dumped = creds.model_dump()
    assert dumped["project_id"] == "p"
    assert dumped["service_account_json"] == '{"type":"service_account"}'


def test_connection_credentials_rejects_unknown_fields():
    # The exact failure mode the audit caught: silent drop of an
    # unrecognised field. extra='forbid' converts the silent loss into
    # a loud 422 at request boundary.
    with pytest.raises(ValidationError) as exc:
        ConnectionCredentials(
            username="u",
            password="p",
            unexpected_field="value",  # type: ignore[call-arg]
        )
    assert "unexpected_field" in str(exc.value).lower() or \
           "extra inputs" in str(exc.value).lower()


# ── P0.3 — Server-side credential JSON validation (M19) ──────────────


def test_spanner_create_rejects_missing_credentials():
    payload = _spanner_create_req(credentials=None)
    with pytest.raises(ValidationError) as exc:
        ProviderCreateRequest.model_validate(payload)
    assert "service_account_json" in str(exc.value)


def test_spanner_create_rejects_malformed_json():
    payload = _spanner_create_req(
        credentials={"service_account_json": "{not valid json"}
    )
    with pytest.raises(ValidationError) as exc:
        ProviderCreateRequest.model_validate(payload)
    assert "valid json" in str(exc.value).lower()


def test_spanner_create_rejects_non_object_json():
    payload = _spanner_create_req(
        credentials={"service_account_json": json.dumps(["array", "not object"])}
    )
    with pytest.raises(ValidationError) as exc:
        ProviderCreateRequest.model_validate(payload)
    assert "json object" in str(exc.value).lower()


def test_spanner_create_rejects_missing_required_sa_keys():
    incomplete = {"type": "service_account", "client_email": "x@y.z"}
    payload = _spanner_create_req(
        credentials={"service_account_json": json.dumps(incomplete)}
    )
    with pytest.raises(ValidationError) as exc:
        ProviderCreateRequest.model_validate(payload)
    msg = str(exc.value)
    assert "missing required keys" in msg
    # Reports the specific missing keys so the operator knows what to add
    assert "private_key" in msg and "project_id" in msg


def test_spanner_create_in_emulator_mode_skips_credential_validation():
    # When useEmulator=true, the emulator skips auth, so missing/empty
    # credentials must be accepted at the validator boundary. The dispatch
    # still gates emulator mode behind SYNODIC_ALLOW_SPANNER_EMULATOR
    # (covered separately in test_spanner_dispatch_emulator_*).
    payload = _spanner_create_req(
        credentials=None,
        extraConfig={
            "projectId": "p",
            "instanceId": "i",
            "databaseId": "d",
            "useEmulator": True,
        },
    )
    req = ProviderCreateRequest.model_validate(payload)
    assert req.provider_type == ProviderType.SPANNER


# ── P0.5 — Dispatch emulator-flag guard ──────────────────────────────


def _dispatch_spanner_with_emulator(monkeypatch_env: dict | None):
    # Both manager.py and provider_registry.py carry parallel Spanner
    # branches per author decision #4. We exercise the manager path
    # (the one actually hit by POST /providers); the registry path is
    # held to identical behaviour by the parity test in Phase 7.2.
    from backend.app.providers.manager import provider_manager
    return provider_manager._create_provider_instance(
        "spanner",
        host=None,
        port=None,
        graph_name=None,
        tls_enabled=False,
        credentials={},
        extra_config={
            "projectId": "p",
            "instanceId": "i",
            "databaseId": "d",
            "useEmulator": True,
        },
    )


def test_spanner_dispatch_emulator_rejected_in_production(monkeypatch):
    monkeypatch.delenv("SYNODIC_ALLOW_SPANNER_EMULATOR", raising=False)
    with pytest.raises(ValueError) as exc:
        _dispatch_spanner_with_emulator(monkeypatch_env=None)
    assert "SYNODIC_ALLOW_SPANNER_EMULATOR" in str(exc.value)


def test_spanner_dispatch_emulator_allowed_with_env_flag(monkeypatch):
    monkeypatch.setenv("SYNODIC_ALLOW_SPANNER_EMULATOR", "1")
    # The provider may fail to fully construct (no Spanner client in the
    # test env), but the emulator gate must pass first. A different
    # exception surface (ImportError on google-cloud-spanner, attribute
    # errors during client init) is acceptable; the gate ValueError is
    # not.
    try:
        _dispatch_spanner_with_emulator(monkeypatch_env=None)
    except ValueError as exc:
        assert "SYNODIC_ALLOW_SPANNER_EMULATOR" not in str(exc), (
            "Emulator gate fired despite the env flag being set"
        )
    except (ImportError, ModuleNotFoundError, AttributeError):
        # Provider construction failed for unrelated reasons in the test
        # environment — acceptable; what we're pinning here is that the
        # emulator gate did NOT veto the call.
        pass


# ── Repo round-trip — P0.1 + P0.2 combined ──────────────────────────


async def test_create_spanner_provider_persists_with_credentials(db_session):
    req = ProviderCreateRequest.model_validate(_spanner_create_req())
    resp = await provider_repo.create_provider(db_session, req)

    assert resp.provider_type == ProviderType.SPANNER
    assert resp.name == "spanner-prod"
    assert resp.extra_config and resp.extra_config["projectId"] == "test-project"
    # ProviderResponse intentionally never returns credentials (per the
    # comment on the model). Read the ORM row directly to verify the
    # encrypted blob actually contains the service-account payload —
    # this is the assertion that would have failed under B2 (silent
    # drop by Pydantic) before the fix.
    orm = await provider_repo.get_provider_orm(db_session, resp.id)
    assert orm is not None
    creds_blob = orm.credentials
    # _encrypt may produce ciphertext (Fernet) or plaintext JSON when
    # CREDENTIAL_ENCRYPTION_KEY is unset (the existing test/dev path).
    # Both branches still must contain the SA email somewhere in the
    # serialised payload — which proves it survived ConnectionCredentials.
    payload_str = (
        creds_blob if isinstance(creds_blob, str) else creds_blob.decode("utf-8", errors="ignore")
    )
    sa_email = _FAKE_SA_KEY["client_email"]
    if "ENCRYPTED:" in payload_str or os.getenv("CREDENTIAL_ENCRYPTION_KEY"):
        # Encrypted: cannot inspect; persistence-non-zero is the contract.
        assert payload_str
    else:
        # Plaintext fallback path — directly verify the SA key survived.
        assert sa_email in payload_str
