import math
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Config:
    oci_config: str = "~/.oci/config"
    profile: str = "DEFAULT"
    compartment_id: str = ""
    subnet_id: str = ""
    image_id: str = ""
    operating_system: str = "Canonical Ubuntu"
    os_version: str = "24.04"
    ssh_key_file: str = "~/.ssh/id_ed25519.pub"
    display_name: str = "ahab-a1"
    hunter_id: str = "ahab-a1"
    ocpus: float = 2.0
    memory_gb: float = 12.0
    boot_volume_gb: int = 50
    assign_public_ip: bool = True
    availability_domains: tuple[str, ...] = ()
    capacity_mode: str = "auto"
    interval_seconds: float = 120.0
    jitter_seconds: float = 30.0
    max_backoff_seconds: float = 900.0
    max_attempts: int = 0
    deadline_hours: float = 0.0
    wait_timeout_seconds: float = 600.0
    state_dir: str = "./state"

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            values = tomllib.load(stream)
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"Unknown options: {sorted(unknown)}")
        for f in fields(cls):
            if f.name not in values:
                continue
            value = values[f.name]
            default = f.default
            if isinstance(default, bool):
                valid = isinstance(value, bool)
            elif isinstance(default, (float, int)):
                valid = (
                    type(value) in (int, float)
                    and math.isfinite(value)
                    and (not isinstance(default, int) or isinstance(value, int))
                )
            elif isinstance(default, tuple):
                valid = isinstance(value, list) and all(isinstance(x, str) for x in value)
            else:
                valid = isinstance(value, str)
            if not valid:
                raise ValueError(f"Invalid type for {f.name}")
        if "availability_domains" in values:
            values["availability_domains"] = tuple(values["availability_domains"])
        result = cls(**values)
        result.validate()
        return result

    def validate(self):
        if self.capacity_mode not in ("auto", "required", "off"):
            raise ValueError("capacity_mode must be auto, required, or off")
        if not self.hunter_id.strip() or not self.display_name.strip():
            raise ValueError("hunter_id and display_name are required")
        if not 1 <= self.ocpus <= 2 or not 1 <= self.memory_gb <= 12:
            raise ValueError(
                "Local limits: 1-2 OCPU, 1-12 GB; free-tier eligibility is not guaranteed"
            )
        if not 1 <= self.memory_gb / self.ocpus <= 64:
            raise ValueError("Invalid RAM/OCPU ratio")
        if not 50 <= self.boot_volume_gb <= 200:
            raise ValueError("boot_volume_gb must be between 50 and 200")
        if self.interval_seconds < 30 or self.jitter_seconds < 0:
            raise ValueError("Minimum interval is 30s; jitter must be nonnegative")
        if self.max_backoff_seconds < self.interval_seconds:
            raise ValueError("max_backoff_seconds must be >= interval_seconds")
        if self.max_attempts < 0 or self.deadline_hours < 0 or self.wait_timeout_seconds < 0:
            raise ValueError("Attempts, deadline, and timeout must be nonnegative")
