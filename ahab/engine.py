import hashlib
import json
import logging
import math
import random
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import oci

LOG = logging.getLogger(__name__)


class FatalError(RuntimeError):
    pass


def classify(error):
    if isinstance(error, oci.exceptions.ServiceError):
        code = (error.code or "").lower()
        message = (error.message or "").lower()
        if code in ("limitexceeded", "quotaexceeded") or error.status in (401, 403):
            return "fatal"
        if (
            "out of host capacity" in message
            or "out of capacity" in message
            or code == "out_of_host_capacity"
        ):
            return "capacity"
        if error.status == 429:
            return "throttled"
        if error.status >= 500 or error.status == 408:
            return "transient"
        return "fatal"
    if isinstance(
        error,
        (
            oci.exceptions.RequestException,
            oci.exceptions.ConnectTimeout,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return "transient"
    return "fatal"


def retry_after(error, now):
    headers = getattr(error, "headers", {}) or {}
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0


class Engine:
    def __init__(
        self,
        config,
        backend,
        store,
        notify=lambda _: None,
        sleep=time.sleep,
        clock=time.time,
        jitter=random.uniform,
    ):
        self.c = config
        self.api = backend
        self.store = store
        self.notify = notify
        self.sleep = sleep
        self.clock = clock
        self.jitter = jitter

    def finish(self, state, instance):
        # Save acceptance before polling or notifying.
        state["instance_id"] = instance.id
        state.pop("pending", None)
        self.store.save(state)
        result = self.api.result(instance, self.c.wait_timeout_seconds)
        self.store.save(result, "result.json")
        LOG.info("Instance found or accepted: %s (%s)", instance.id, result["state"])
        if not state.get("notified"):
            self.notify(result)
            state["notified"] = True
            self.store.save(state)
        return result

    def run(self, plan):
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        state = self.store.load()
        if state and state.get("plan_hash") != digest:
            raise FatalError("The plan differs from the journal. Review state before continuing.")
        if not state:
            state = {
                "plan_hash": digest,
                "plan": plan,
                "attempts": 0,
                "ad_index": 0,
                "started_at": self.clock(),
                "backoff": self.c.interval_seconds,
            }
            self.store.save(state)
        if state.get("instance_id"):
            return self.finish(state, self.api.get_instance(state["instance_id"]))
        ads = plan["ads"]
        if not ads:
            raise FatalError("No compatible ADs found")
        while True:
            if self.limit_reached(state):
                LOG.warning("Search limit reached; journal preserved")
                return None
            # Preserve pacing without sleeping past the deadline.
            remaining = state.get("next_at", 0) - self.clock()
            if self.c.deadline_hours:
                remaining = min(
                    remaining, state["started_at"] + self.c.deadline_hours * 3600 - self.clock()
                )
            if remaining > 0:
                self.sleep(remaining)
            if self.limit_reached(state):
                return None
            state["attempts"] += 1
            try:
                existing = self.api.find_existing()
            except Exception as exc:
                if classify(exc) not in ("transient", "throttled"):
                    raise
                state["backoff"] = min(state["backoff"] * 2, self.c.max_backoff_seconds)
                LOG.warning("Instance reconciliation failed; retrying without launching")
                self.pace(state, exc)
                continue
            if existing is not None:
                return self.finish(state, existing)
            pending = state.get("pending")
            if pending and self.clock() - pending["created_at"] >= 23 * 3600:
                raise FatalError(
                    "Uncertain launch is over 23 hours old; reconcile manually. "
                    "The token will not be renewed automatically."
                )
            ad = ads[state["ad_index"] % len(ads)]
            error = None
            try:
                if pending is None:
                    status = self.api.capacity(ad)
                    LOG.info("Check #%s AD=%s capacity=%s", state["attempts"], ad, status)
                    if status == "unavailable":
                        state["ad_index"] += 1
                        state["backoff"] = self.c.interval_seconds
                        self.pace(state)
                        continue
                    if status == "unsupported":
                        raise FatalError(
                            f"Capacity Report does not support this shape/config in {ad}; "
                            "check configuration or set capacity_mode=off"
                        )
                    pending = {
                        "token": uuid.uuid4().hex,
                        "created_at": self.clock(),
                        "payload": self.api.payload(ad),
                    }
                    state["pending"] = pending
                    self.store.save(state)  # Save before launching.
                instance = self.api.launch(pending["payload"], pending["token"])
            except Exception as exc:
                reason = classify(exc)
                if reason == "fatal":
                    if isinstance(exc, oci.exceptions.ServiceError):
                        raise FatalError(
                            f"OCI HTTP {exc.status} {exc.code}; "
                            "check authentication, quotas, and configuration"
                        ) from exc
                    raise
                error = exc
                LOG.warning("Attempt #%s AD=%s: %s", state["attempts"], ad, reason)
                # Rotate only after definite capacity failure; otherwise retain request and token.
                if reason == "capacity":
                    state.pop("pending", None)
                    state["ad_index"] += 1
                    state["backoff"] = self.c.interval_seconds
                else:
                    state["backoff"] = min(state["backoff"] * 2, self.c.max_backoff_seconds)
                self.pace(state, error)
            else:
                return self.finish(state, instance)

    def limit_reached(self, state):
        return (self.c.max_attempts and state["attempts"] >= self.c.max_attempts) or (
            self.c.deadline_hours
            and self.clock() - state["started_at"] >= self.c.deadline_hours * 3600
        )

    def pace(self, state, error=None):
        delay = max(state["backoff"], retry_after(error, self.clock()))
        delay += self.jitter(0, self.c.jitter_seconds)
        state["next_at"] = self.clock() + delay
        state["updated_at"] = datetime.now(UTC).isoformat()
        self.store.save(state)
        LOG.info("Next check in %.1fs", delay)
