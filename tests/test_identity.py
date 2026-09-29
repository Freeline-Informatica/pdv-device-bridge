import json
import uuid

import pytest

from pdv_device_bridge.identity import load_or_create_identity


def test_prelinked_provision_replaces_only_an_unbound_identity(tmp_path):
    state = tmp_path / "identity.json"
    provision = tmp_path / "provision.json"
    generated = load_or_create_identity(state, provision)
    chosen = str(uuid.uuid4())
    provision.write_text(json.dumps({
        "device_id": chosen,
        "site_id": str(uuid.uuid4()),
        "code": "CLIENT1",
        "hostname": "freeline-bridge-client1",
        "enrollment_token": "x" * 64,
    }))

    enrolled = load_or_create_identity(state, provision)

    assert enrolled.device_id == chosen
    assert enrolled.created_at == generated.created_at
    assert enrolled.enrollment_token == "x" * 64
    assert not provision.exists()
    assert load_or_create_identity(state).device_id == chosen


def test_bound_identity_refuses_another_uuid(tmp_path):
    state = tmp_path / "identity.json"
    provision = tmp_path / "provision.json"
    first = str(uuid.uuid4())
    provision.write_text(json.dumps({"device_id": first, "site_id": str(uuid.uuid4()), "enrollment_token": "x" * 64}))
    load_or_create_identity(state, provision)
    provision.write_text(json.dumps({"device_id": str(uuid.uuid4()), "site_id": str(uuid.uuid4())}))

    with pytest.raises(ValueError, match="UUID"):
        load_or_create_identity(state, provision)
    assert provision.exists()
