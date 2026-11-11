import json
from dataclasses import replace
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import oci
import pytest

from ahab import cli
from ahab.config import Config
from ahab.engine import FatalError
from ahab.oracle import Oracle


def adapter(scope="AD"):
    api = Oracle.__new__(Oracle)
    api.c = Config()
    api.config = {"tenancy": "tenancy"}
    api.compartment = "child-compartment"
    api.limits = Mock()
    api.all = Mock(
        side_effect=lambda method, compartment, **kw: [
            oci.limits.models.LimitDefinitionSummary(
                name=kw["name"], scope_type=scope, is_resource_availability_supported=True
            )
        ]
    )
    return api


def availability(value, fractional=None):
    return Obj(
        data=oci.limits.models.ResourceAvailability(
            available=value, fractional_availability=fractional
        )
    )


def test_ad_quota_requires_cpu_and_memory_in_same_ad():
    api = adapter()
    api.limits.get_resource_availability.side_effect = [
        availability(2),
        availability(0),
        availability(0),
        availability(12),
    ]
    result = api.check_limits(["AD1", "AD2"])
    assert not result["sufficient"]
    assert all(not row["sufficient"] for row in result["availability_domains"])
    api.limits.get_resource_availability.assert_any_call(
        "compute", "standard-a1-core-count", "child-compartment", availability_domain="AD1"
    )
    api.all.assert_any_call(
        api.limits.list_limit_definitions,
        "tenancy",
        service_name="compute",
        name="standard-a1-memory-count",
    )


def test_one_ad_with_headroom_is_enough():
    api = adapter()
    api.limits.get_resource_availability.side_effect = [
        availability(0),
        availability(2),
        availability(12),
        availability(12),
    ]
    assert api.check_limits(["AD1", "AD2"])["sufficient"]


@pytest.mark.parametrize("scope", ["REGION", "GLOBAL"])
def test_non_ad_limits_are_not_requested_with_ad(scope):
    api = adapter(scope)
    api.limits.get_resource_availability.side_effect = [availability(2), availability(12)]
    result = api.check_limits(["AD1", "AD2"])
    assert result["sufficient"]
    assert api.limits.get_resource_availability.call_count == 2
    for call in api.limits.get_resource_availability.call_args_list:
        assert call.kwargs == {}


def test_fractional_availability_takes_precedence():
    api = adapter("REGION")
    api.c = replace(api.c, ocpus=1.5)
    api.limits.get_resource_availability.side_effect = [availability(1, 1.5), availability(12)]
    assert api.check_limits(["AD1"])["sufficient"]


@pytest.mark.parametrize("value", [None, float("nan"), float("inf")])
def test_missing_or_invalid_availability_is_not_success(value):
    api = adapter()
    api.limits.get_resource_availability.return_value = availability(value)
    with pytest.raises(FatalError, match="no availability value"):
        api.check_limits(["AD1"])


def test_missing_definition_is_not_success():
    api = adapter()
    api.all.return_value = []
    api.all.side_effect = None
    with pytest.raises(FatalError, match="missing or ambiguous"):
        api.check_limits(["AD1"])
    api.limits.get_resource_availability.assert_not_called()


def test_unsupported_scope_is_not_success():
    api = adapter("UNKNOWN_ENUM_VALUE")
    with pytest.raises(FatalError, match="Cannot verify"):
        api.check_limits(["AD1"])


def test_permissions_error_is_not_zero_or_unlimited_quota():
    api = adapter()
    api.limits.get_resource_availability.side_effect = oci.exceptions.ServiceError(
        403, "NotAuthorized", {}, "denied"
    )
    with pytest.raises(oci.exceptions.ServiceError):
        api.check_limits(["AD1"])


@pytest.mark.parametrize(
    "sufficient,existing,exit_code", [(True, False, 0), (False, False, 1), (False, True, 0)]
)
def test_doctor_limits_never_launch(tmp_path, monkeypatch, capsys, sufficient, existing, exit_code):
    path = tmp_path / "config.toml"
    path.write_text("")
    api = Mock()
    api.prepare.return_value = {"ads": ["AD1"], "ssh_public_key": "SECRET"}
    api.find_existing.return_value = Obj(id="vm1") if existing else None
    api.check_limits.return_value = {"sufficient": sufficient, "availability_domains": []}
    monkeypatch.setattr(cli, "Oracle", lambda _: api)
    monkeypatch.setattr(cli, "validate_environment", lambda: None)
    monkeypatch.setattr("sys.argv", ["ahab", "doctor", "--config", str(path), "--check-limits"])
    assert cli.main() == exit_code
    output = capsys.readouterr().out
    assert "SECRET" not in output
    assert json.loads(output)["new_instance_quota"]["sufficient"] is sufficient
    api.launch.assert_not_called()
    api.capacity.assert_not_called()


def test_limits_flag_is_not_silently_ignored(monkeypatch):
    monkeypatch.setattr("sys.argv", ["ahab", "run", "--check-limits"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
