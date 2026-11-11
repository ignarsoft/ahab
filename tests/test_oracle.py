from dataclasses import replace
from types import SimpleNamespace as Obj
from unittest.mock import Mock

import oci
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ahab.config import Config
from ahab.engine import FatalError
from ahab.oracle import SHAPE, Oracle


def adapter(mode="auto"):
    api = Oracle.__new__(Oracle)
    api.c = replace(Config(), capacity_mode=mode, subnet_id="subnet1")
    api.compartment = "compartment1"
    api.compute = Mock()
    api.network = Mock()
    api.report_disabled = False
    api.plan = {"image_id": "image1"}
    api.ssh_key = "ssh-ed25519 AAAA"
    return api


def report(api, *statuses):
    api.compute.create_compute_capacity_report.return_value.data = (
        oci.core.models.ComputeCapacityReport(
            shape_availabilities=[
                oci.core.models.CapacityReportShapeAvailability(
                    instance_shape=SHAPE, availability_status=status, available_count=count
                )
                for status, count in statuses
            ]
        )
    )


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([("AVAILABLE", 1)], "available"),
        ([("AVAILABLE", None)], "available"),
        ([("AVAILABLE", 0)], "unavailable"),
        ([("OUT_OF_HOST_CAPACITY", 0)], "unavailable"),
        ([("HARDWARE_NOT_SUPPORTED", 0)], "unsupported"),
        ([("OUT_OF_HOST_CAPACITY", 0), ("AVAILABLE", 2)], "available"),
        ([], "unknown"),
    ],
)
def test_capacity_statuses(rows, expected):
    api = adapter()
    report(api, *rows)
    assert api.capacity("AD1") == expected
    details = api.compute.create_compute_capacity_report.call_args.args[0]
    assert details.availability_domain == "AD1"
    assert details.shape_availabilities[0].instance_shape_config.ocpus == 2
    assert details.shape_availabilities[0].fault_domain is None


@pytest.mark.parametrize(
    "mode,status,fallback",
    [
        ("auto", 404, True),
        ("auto", 403, True),
        ("required", 404, False),
        ("auto", 401, False),
        ("auto", 429, False),
        ("auto", 500, False),
        ("auto", 400, False),
    ],
)
def test_capacity_fallback(mode, status, fallback):
    api = adapter(mode)
    api.compute.create_compute_capacity_report.side_effect = oci.exceptions.ServiceError(
        status, "Error", {}, "test"
    )
    if fallback:
        assert api.capacity("AD1") == "unknown"
        assert api.report_disabled
        assert api.capacity("AD2") == "unknown"
        assert api.compute.create_compute_capacity_report.call_count == 1
    else:
        with pytest.raises(oci.exceptions.ServiceError):
            api.capacity("AD1")


def test_off_no_report():
    api = adapter("off")
    assert api.capacity("AD1") == "unknown"
    api.compute.create_compute_capacity_report.assert_not_called()


def test_required_empty_report():
    api = adapter("required")
    report(api)
    with pytest.raises(FatalError):
        api.capacity("AD1")


def test_sdk_launch_models_and_token():
    api = adapter()
    payload = api.payload("AD1")
    api.launch(payload, "token123")
    args = api.compute.launch_instance.call_args
    details = args.args[0]
    assert isinstance(details.source_details, oci.core.models.InstanceSourceViaImageDetails)
    assert details.source_details.image_id == "image1"
    assert details.shape_config.memory_in_gbs == 12
    assert details.instance_options.are_legacy_imds_endpoints_disabled
    assert args.kwargs == {"opc_retry_token": "token123"}
    assert "fault_domain" not in payload


def test_nsgs_reach_sdk_and_saved_payload_is_independent_of_config():
    api = adapter()
    nsg = "ocid1.networksecuritygroup.oc1.test"
    api.c = replace(api.c, nsg_ids=(nsg,))
    payload = api.payload("AD1")
    assert payload["create_vnic_details"]["nsg_ids"] == [nsg]
    api.c = replace(api.c, nsg_ids=())
    api.launch(payload, "same-token")
    details = api.compute.launch_instance.call_args.args[0]
    assert details.create_vnic_details.nsg_ids == [nsg]
    assert "nsg_ids" not in api.payload("AD1")["create_vnic_details"]


@pytest.mark.parametrize(
    "state,vcn,message",
    [("TERMINATING", "vcn1", "not AVAILABLE"), ("AVAILABLE", "vcn2", "subnet VCN")],
)
def test_prepare_rejects_unusable_nsg_before_launch(state, vcn, message):
    api = adapter()
    api.c = replace(api.c, nsg_ids=("ocid1.networksecuritygroup.oc1.test",))
    api.config = {"tenancy": "tenancy", "region": "home"}
    api.identity = Mock()
    api.identity.list_region_subscriptions.return_value.data = [
        Obj(is_home_region=True, region_name="home")
    ]
    api.network.get_subnet.return_value.data = Obj(
        lifecycle_state="AVAILABLE", prohibit_public_ip_on_vnic=False, vcn_id="vcn1"
    )
    api.network.get_network_security_group.return_value.data = Obj(
        lifecycle_state=state, vcn_id=vcn
    )
    with pytest.raises(FatalError, match=message):
        api.prepare()
    api.compute.launch_instance.assert_not_called()
    api.compute.create_compute_capacity_report.assert_not_called()


def test_existing_instance_other_shape_does_not_stop_hunt():
    api = adapter()
    api.all = Mock(
        return_value=[
            Obj(
                lifecycle_state="RUNNING", display_name="unrelated", freeform_tags={}, shape="other"
            )
        ]
    )
    assert api.find_existing() is None


def test_stopped_instance_counts_as_existing():
    api = adapter()
    instance = Obj(lifecycle_state="STOPPED", display_name="ahab-a1", freeform_tags={}, shape=SHAPE)
    api.all = Mock(return_value=[instance])
    assert api.find_existing() is instance


def test_prepare_home_region_guard():
    api = adapter()
    api.config = {"tenancy": "tenancy1", "region": "wrong-region"}
    api.identity = Mock()
    api.identity.list_region_subscriptions.return_value.data = [
        Obj(is_home_region=True, region_name="home-region")
    ]
    with pytest.raises(FatalError, match="home region"):
        api.prepare()
    api.compute.launch_instance.assert_not_called()


@pytest.mark.parametrize("nsg_ids", [(), ("ocid1.networksecuritygroup.oc1.test",)])
@pytest.mark.parametrize("valid_key", [True, False])
def test_prepare_pins_image_and_ad_specific_subnet(tmp_path, valid_key, nsg_ids):
    api = adapter()
    key = tmp_path / "ssh.pub"
    public = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH)
        .decode()
    )
    key.write_text(public if valid_key else "ssh-ed25519 AAAA comment", encoding="utf-8")
    api.c = replace(api.c, ssh_key_file=str(key), nsg_ids=nsg_ids)
    api.config = {"tenancy": "tenancy1", "region": "home-region"}
    api.identity = Mock()
    api.identity.list_region_subscriptions.return_value.data = [
        Obj(is_home_region=True, region_name="home-region")
    ]
    api.identity.list_availability_domains.return_value.data = [Obj(name="AD1"), Obj(name="AD2")]
    api.network.get_subnet.return_value.data = Obj(
        lifecycle_state="AVAILABLE",
        prohibit_public_ip_on_vnic=False,
        availability_domain="AD2",
        vcn_id="vcn1",
    )
    api.network.get_network_security_group.return_value.data = Obj(
        lifecycle_state="AVAILABLE", vcn_id="vcn1"
    )
    api.compute.get_image.return_value.data = Obj(lifecycle_state="AVAILABLE", size_in_mbs=1024)
    api.all = Mock(return_value=[Obj(shape=SHAPE)])
    if not valid_key:
        with pytest.raises(FatalError, match="Invalid SSH public key"):
            api.prepare()
        api.compute.launch_instance.assert_not_called()
        return
    plan = api.prepare({"image_id": "pinned-image"})
    assert plan["ads"] == ["AD2"]
    assert plan["image_id"] == "pinned-image"
    if nsg_ids:
        assert plan["nsg_ids"] == list(nsg_ids)
        api.network.get_network_security_group.assert_called_once_with(nsg_ids[0])
    else:
        assert "nsg_ids" not in plan
        api.network.get_network_security_group.assert_not_called()
    api.compute.list_images.assert_not_called()
    api.compute.create_compute_capacity_report.assert_not_called()
    api.compute.launch_instance.assert_not_called()
