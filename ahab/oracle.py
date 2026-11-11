"""OCI SDK adapter with read-only discovery."""

import logging
import math
import time
from pathlib import Path

import oci
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import load_ssh_public_key

from .engine import FatalError, classify

LOG = logging.getLogger(__name__)
SHAPE = "VM.Standard.A1.Flex"
TAG = "ahab-hunter-id"


class Oracle:
    def __init__(self, settings):
        self.c = settings
        self.config = oci.config.from_file(
            str(Path(settings.oci_config).expanduser()), settings.profile
        )
        oci.config.validate_config(self.config)
        # The engine controls retries and idempotency.
        options = {
            "retry_strategy": oci.retry.NoneRetryStrategy(),
            "circuit_breaker_strategy": oci.circuit_breaker.NoCircuitBreakerStrategy(),
            "timeout": (10, 60),
        }
        self.compute = oci.core.ComputeClient(self.config, **options)
        self.network = oci.core.VirtualNetworkClient(self.config, **options)
        self.identity = oci.identity.IdentityClient(self.config, **options)
        self.limits = oci.limits.LimitsClient(self.config, **options)
        self.compartment = settings.compartment_id or self.config["tenancy"]
        self.report_disabled = False

    @staticmethod
    def all(method, *args, **kwargs):
        return oci.pagination.list_call_get_all_results(method, *args, **kwargs).data

    def discover(self):
        return {
            "region": self.config["region"],
            "compartment_id": self.compartment,
            "availability_domains": [
                ad.name
                for ad in self.identity.list_availability_domains(self.config["tenancy"]).data
            ],
            "subnets": [
                {
                    "id": sn.id,
                    "name": sn.display_name,
                    "availability_domain": sn.availability_domain,
                    "public_ip_allowed": not sn.prohibit_public_ip_on_vnic,
                }
                for sn in self.all(self.network.list_subnets, self.compartment)
                if sn.lifecycle_state == "AVAILABLE"
            ],
            "images": [{"id": im.id, "name": im.display_name} for im in self.images()],
        }

    def images(self):
        return [
            im
            for im in self.all(
                self.compute.list_images,
                self.compartment,
                shape=SHAPE,
                operating_system=self.c.operating_system,
                operating_system_version=self.c.os_version,
                sort_by="TIMECREATED",
                sort_order="DESC",
            )
            if im.lifecycle_state == "AVAILABLE"
        ]

    def prepare(self, saved_plan=None):
        regions = self.identity.list_region_subscriptions(self.config["tenancy"]).data
        if not any(r.is_home_region and r.region_name == self.config["region"] for r in regions):
            raise FatalError("The profile must use the tenancy home region")
        if not self.c.subnet_id:
            raise FatalError("Select subnet_id explicitly using discover")
        subnet = self.network.get_subnet(self.c.subnet_id).data
        if subnet.lifecycle_state != "AVAILABLE":
            raise FatalError("The subnet is not AVAILABLE")
        if self.c.assign_public_ip and subnet.prohibit_public_ip_on_vnic:
            raise FatalError("The subnet does not allow public IPs")
        key = Path(self.c.ssh_key_file).expanduser().read_text(encoding="utf-8").strip()
        parts = key.split()
        if (
            len(key.splitlines()) != 1
            or len(parts) < 2
            or not parts[0].startswith(("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-"))
        ):
            raise FatalError("An OpenSSH public key is required, not a private key")
        try:
            load_ssh_public_key(key.encode("utf-8"))
        except (ValueError, UnsupportedAlgorithm) as exc:
            raise FatalError("Invalid SSH public key") from exc
        self.ssh_key = key
        ads = [
            ad.name for ad in self.identity.list_availability_domains(self.config["tenancy"]).data
        ]
        if self.c.availability_domains:
            if set(self.c.availability_domains) - set(ads):
                raise FatalError("Configured ADs do not belong to this region or tenancy")
            ads = list(dict.fromkeys(self.c.availability_domains))
        if subnet.availability_domain:
            ads = [ad for ad in ads if ad == subnet.availability_domain]
        # Keep the selected image across restarts.
        image_id = self.c.image_id or (saved_plan or {}).get("image_id")
        if not image_id:
            images = self.images()
            if not images:
                raise FatalError("No A1-compatible image found for this OS and version")
            image_id = images[0].id
        image = self.compute.get_image(image_id).data
        if image.lifecycle_state != "AVAILABLE":
            raise FatalError("The image is not AVAILABLE")
        if image.size_in_mbs and self.c.boot_volume_gb * 1024 < image.size_in_mbs:
            raise FatalError("The boot volume is smaller than the image minimum")
        compatible = []
        for ad in ads:
            shapes = self.all(
                self.compute.list_shapes,
                self.compartment,
                availability_domain=ad,
                image_id=image_id,
            )
            if any(s.shape == SHAPE for s in shapes):
                compatible.append(ad)
        if not compatible:
            raise FatalError("No AD supports the selected image and shape")
        self.plan = {
            "tenancy": self.config["tenancy"],
            "region": self.config["region"],
            "compartment_id": self.compartment,
            "ads": compatible,
            "image_id": image_id,
            "subnet_id": self.c.subnet_id,
            "ocpus": self.c.ocpus,
            "memory_gb": self.c.memory_gb,
            "boot_volume_gb": self.c.boot_volume_gb,
            "assign_public_ip": self.c.assign_public_ip,
            "hunter_id": self.c.hunter_id,
            "display_name": self.c.display_name,
            "ssh_public_key": key,
        }
        return self.plan

    def check_limits(self, ads):
        """Read compute quota headroom; this does not measure host capacity or cost."""
        if not ads:
            raise FatalError("No ADs to check for quota availability")
        resources = {}
        for field, name in (
            ("ocpus", "standard-a1-core-count"),
            ("memory_gb", "standard-a1-memory-count"),
        ):
            definitions = self.all(
                self.limits.list_limit_definitions,
                self.config["tenancy"],
                service_name="compute",
                name=name,
            )
            definitions = [d for d in definitions if d.name == name]
            if len(definitions) != 1:
                raise FatalError(f"Cannot verify quota: missing or ambiguous limit {name}")
            definition = definitions[0]
            scope = definition.scope_type
            if not definition.is_resource_availability_supported or scope not in (
                "AD",
                "REGION",
                "GLOBAL",
            ):
                raise FatalError(f"Cannot verify quota availability for {name}")
            values = {}
            for ad in ads if scope == "AD" else [None]:
                kwargs = {"availability_domain": ad} if ad is not None else {}
                data = self.limits.get_resource_availability(
                    "compute", name, self.compartment, **kwargs
                ).data
                available = data.fractional_availability
                if available is None:
                    available = data.available
                if type(available) not in (int, float) or not math.isfinite(available):
                    raise FatalError(f"Cannot verify quota: no availability value for {name}")
                values[ad] = available
            resources[field] = {ad: values[ad if scope == "AD" else None] for ad in ads}
        rows = [
            {
                "availability_domain": ad,
                "ocpus_available": resources["ocpus"][ad],
                "memory_gb_available": resources["memory_gb"][ad],
                "sufficient": (
                    resources["ocpus"][ad] >= self.c.ocpus
                    and resources["memory_gb"][ad] >= self.c.memory_gb
                ),
            }
            for ad in ads
        ]
        return {"sufficient": any(row["sufficient"] for row in rows), "availability_domains": rows}

    def find_existing(self):
        matches = [
            im
            for im in self.all(self.compute.list_instances, self.compartment)
            if im.lifecycle_state not in ("TERMINATED", "TERMINATING")
            and (
                (im.freeform_tags or {}).get(TAG) == self.c.hunter_id
                or im.display_name == self.c.display_name
            )
        ]
        if len(matches) > 1:
            raise FatalError("Multiple matching instances found; review manually")
        if matches:
            if matches[0].shape != SHAPE:
                raise FatalError("Name or tag matches an instance with a different shape")
            return matches[0]
        return None

    def capacity(self, ad):
        if self.c.capacity_mode == "off" or self.report_disabled:
            return "unknown"
        models = oci.core.models
        details = models.CreateComputeCapacityReportDetails(
            compartment_id=self.compartment,
            availability_domain=ad,
            shape_availabilities=[
                models.CreateCapacityReportShapeAvailabilityDetails(
                    instance_shape=SHAPE,
                    instance_shape_config=models.CapacityReportInstanceShapeConfig(
                        ocpus=self.c.ocpus, memory_in_gbs=self.c.memory_gb
                    ),
                )
            ],
        )
        try:
            report = self.compute.create_compute_capacity_report(details).data
        except oci.exceptions.ServiceError as exc:
            # Only unsupported or inaccessible reports permit fallback.
            if self.c.capacity_mode == "auto" and (
                exc.status in (403, 404, 405, 501)
                or exc.code in ("NotSupported", "UnsupportedOperation")
            ):
                self.report_disabled = True
                LOG.warning(
                    "Capacity Report unavailable (%s); falling back to LaunchInstance", exc.code
                )
                return "unknown"
            raise
        rows = report.shape_availabilities or []
        rows = [r for r in rows if r.instance_shape == SHAPE]
        if any(
            r.availability_status == "AVAILABLE"
            and (r.available_count is None or r.available_count > 0)
            for r in rows
        ):
            return "available"
        if rows and all(
            r.availability_status == "OUT_OF_HOST_CAPACITY"
            or (r.availability_status == "AVAILABLE" and r.available_count == 0)
            for r in rows
        ):
            return "unavailable"
        if rows and all(r.availability_status == "HARDWARE_NOT_SUPPORTED" for r in rows):
            return "unsupported"
        if self.c.capacity_mode == "required":
            raise FatalError("Empty or unknown Capacity Report in required mode")
        LOG.warning("Inconclusive Capacity Report; attempting launch")
        return "unknown"

    def payload(self, ad):
        return {
            "availability_domain": ad,
            "compartment_id": self.compartment,
            "display_name": self.c.display_name,
            "shape": SHAPE,
            "shape_config": {"ocpus": self.c.ocpus, "memory_in_gbs": self.c.memory_gb},
            "source_details": {
                "source_type": "image",
                "image_id": self.plan["image_id"],
                "boot_volume_size_in_gbs": self.c.boot_volume_gb,
            },
            "create_vnic_details": {
                "subnet_id": self.c.subnet_id,
                "assign_public_ip": self.c.assign_public_ip,
            },
            "metadata": {"ssh_authorized_keys": self.ssh_key},
            "freeform_tags": {TAG: self.c.hunter_id},
            "instance_options": {"are_legacy_imds_endpoints_disabled": True},
        }

    def launch(self, payload, token):
        # Build nested models explicitly for the polymorphic image source.
        models = oci.core.models
        details = models.LaunchInstanceDetails(
            **{
                k: v
                for k, v in payload.items()
                if k
                not in ("shape_config", "source_details", "create_vnic_details", "instance_options")
            },
            shape_config=models.LaunchInstanceShapeConfigDetails(**payload["shape_config"]),
            source_details=models.InstanceSourceViaImageDetails(**payload["source_details"]),
            create_vnic_details=models.CreateVnicDetails(**payload["create_vnic_details"]),
            instance_options=models.InstanceOptions(**payload["instance_options"]),
        )
        return self.compute.launch_instance(details, opc_retry_token=token).data

    def get_instance(self, instance_id):
        return self.compute.get_instance(instance_id).data

    def result(self, instance, timeout):
        end = time.monotonic() + timeout
        while instance.lifecycle_state == "PROVISIONING":
            if time.monotonic() >= end:
                break
            time.sleep(min(10, max(0, end - time.monotonic())))
            try:
                instance = self.get_instance(instance.id)
            except Exception as exc:
                if classify(exc) == "fatal":
                    raise
                LOG.warning("State check failed; instance was already accepted")
        public_ip = None
        private_ip = None
        try:
            vnics = self.all(
                self.compute.list_vnic_attachments, self.compartment, instance_id=instance.id
            )
            for attachment in vnics:
                if attachment.lifecycle_state == "ATTACHED":
                    vnic = self.network.get_vnic(attachment.vnic_id).data
                    public_ip = vnic.public_ip or public_ip
                    private_ip = vnic.private_ip or private_ip
        except oci.exceptions.ServiceError:
            LOG.warning("Cannot retrieve IPs; check the OCI console")
        return {
            "instance_id": instance.id,
            "display_name": instance.display_name,
            "state": instance.lifecycle_state,
            "availability_domain": instance.availability_domain,
            "region": self.config["region"],
            "public_ip": public_ip,
            "private_ip": private_ip,
            "console_url": f"https://cloud.oracle.com/compute/instances/{instance.id}"
            f"?region={self.config['region']}",
        }
