import pytest

from ahab.config import Config


@pytest.mark.parametrize(
    "text",
    [
        "interval_seconds = 10",
        'capacity_mode = "bad"',
        "max_attempts = -1",
        'unknown_option = "x"',
        'assign_public_ip = "true"',
        "ocpus = true",
        "availability_domains = [1]",
        "memory_gb = 24",
        "interval_seconds = nan",
        "nsg_ids = [1]",
        'nsg_ids = ["ocid1.subnet.oc1.test"]',
        'nsg_ids = ["ocid1.networksecuritygroup.oc1.test", "ocid1.networksecuritygroup.oc1.test"]',
        'nsg_ids = [" ocid1.networksecuritygroup.oc1.test"]',
    ],
)
def test_bad_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(path)


def test_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    config = Config.load(path)
    assert (config.ocpus, config.memory_gb, config.boot_volume_gb) == (2, 12, 50)
    assert config.interval_seconds == 120


def test_nsg_limit_and_tuple_conversion(tmp_path):
    import json

    path = tmp_path / "config.toml"
    ids = [f"ocid1.networksecuritygroup.oc1.test{i}" for i in range(6)]
    path.write_text(f"nsg_ids = {json.dumps(ids)}")
    with pytest.raises(ValueError, match="at most five"):
        Config.load(path)
    path.write_text(f"nsg_ids = {json.dumps(ids[:5])}")
    assert Config.load(path).nsg_ids == tuple(ids[:5])


def test_fractional_ocpus(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'ocpus = 1.5\nmemory_gb = 9.0\navailability_domains = ["AD1"]', encoding="utf-8"
    )
    config = Config.load(path)
    assert config.ocpus == 1.5
    assert config.availability_domains == ("AD1",)
