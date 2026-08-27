import json

from pdv_device_bridge.identity import load_or_create_identity


def test_identity_consumes_provisioning_and_remains_stable(tmp_path) -> None:
    state = tmp_path / "identity.json"
    provision = tmp_path / "provision.json"
    provision.write_text(json.dumps({
        "device_id": "12345678-1234-4234-8234-1234567890ab",
        "code": "AB12CD",
        "hostname": "freeline-bridge-ab12cd",
        "site_id": "site-1",
        "enrollment_token": "secret",
    }))

    created = load_or_create_identity(state, provision)
    loaded = load_or_create_identity(state, provision)

    assert created == loaded
    assert created.hostname == "freeline-bridge-ab12cd"
    assert created.site_id == "site-1"
    assert provision.exists() is False
    assert state.stat().st_mode & 0o777 == 0o600


def test_identity_generates_uuid_and_code(tmp_path) -> None:
    identity = load_or_create_identity(tmp_path / "identity.json")

    assert len(identity.device_id) == 36
    assert len(identity.code) == 6
    assert identity.hostname == f"freeline-bridge-{identity.code.lower()}"
