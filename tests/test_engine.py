from dataclasses import replace
from types import SimpleNamespace

import oci
import pytest

from ahab.config import Config
from ahab.engine import Engine, FatalError, classify, retry_after
from ahab.storage import Store

INSTANCE = SimpleNamespace(id="vm1")
PLAN = {"ads": ["AD1", "AD2"], "image_id": "image1"}


def service(status=500, code="InternalError", message="error", headers=None):
    return oci.exceptions.ServiceError(status, code, headers or {}, message)


class Backend:
    def __init__(self, outcomes=(), reports=()):
        self.outcomes = iter(outcomes)
        self.reports = iter(reports)
        self.launches = []
        self.queries = []
        self.existing = None

    def find_existing(self):
        return self.existing

    def get_instance(self, instance_id):
        assert instance_id == INSTANCE.id
        return INSTANCE

    def result(self, instance, timeout):
        return {"instance_id": instance.id, "state": "RUNNING"}

    def capacity(self, ad):
        self.queries.append(ad)
        value = next(self.reports, "available")
        if isinstance(value, Exception):
            raise value
        return value

    def payload(self, ad):
        return {"ad": ad}

    def launch(self, payload, token):
        self.launches.append((payload.copy(), token))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Clock:
    def __init__(self):
        self.now = 10000
        self.waits = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def engine(tmp_path, api, config=None, clock=None, notify=lambda _: None):
    clock = clock or Clock()
    return Engine(
        config or Config(),
        api,
        Store(tmp_path),
        notify=notify,
        clock=clock.time,
        sleep=clock.sleep,
        jitter=lambda a, b: 0,
    )


@pytest.mark.parametrize(
    "error,reason",
    [
        (service(message="Out of host capacity."), "capacity"),
        (service(400, "OUT_OF_HOST_CAPACITY"), "capacity"),
        (service(400, "LimitExceeded"), "fatal"),
        (service(400, "QuotaExceeded"), "fatal"),
        (service(401, "NotAuthenticated"), "fatal"),
        (service(404, "NotAuthorizedOrNotFound"), "fatal"),
        (service(429, "TooManyRequests"), "throttled"),
        (service(503), "transient"),
        (TimeoutError(), "transient"),
        (oci.exceptions.RequestException("reset"), "transient"),
        (ValueError(), "fatal"),
    ],
)
def test_classification(error, reason):
    assert classify(error) == reason


def test_rotation(tmp_path):
    api = Backend([service(message="Out of host capacity."), INSTANCE])
    hunter = engine(tmp_path, api)
    assert hunter.run(PLAN)["instance_id"] == "vm1"
    assert [p[0]["ad"] for p in api.launches] == ["AD1", "AD2"]
    assert api.launches[0][1] != api.launches[1][1]
    assert hunter.store.load()["instance_id"] == "vm1"


@pytest.mark.parametrize("error", [TimeoutError(), service(503), service(429)])
def test_uncertain_launch_same_request(tmp_path, error):
    api = Backend([error, INSTANCE])
    engine(tmp_path, api).run(PLAN)
    assert api.launches[0] == api.launches[1]
    assert api.queries == ["AD1"]


def test_capacity_gate_no_launch_and_rotates(tmp_path):
    api = Backend([INSTANCE], ["unavailable", "available"])
    engine(tmp_path, api).run(PLAN)
    assert api.queries == ["AD1", "AD2"]
    assert len(api.launches) == 1


def test_restart_after_interrupted_launch(tmp_path):
    api = Backend([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        engine(tmp_path, api).run(PLAN)
    pending = Store(tmp_path).load()["pending"]
    resumed = Backend([INSTANCE])
    engine(tmp_path, resumed).run(PLAN)
    assert resumed.queries == []
    assert resumed.launches == [(pending["payload"], pending["token"])]


def test_expired_pending_never_new_token(tmp_path):
    api = Backend([KeyboardInterrupt()])
    clock = Clock()
    with pytest.raises(KeyboardInterrupt):
        engine(tmp_path, api, clock=clock).run(PLAN)
    clock.now += 24 * 3600
    resumed = Backend([])
    with pytest.raises(FatalError, match="23 hours"):
        engine(tmp_path, resumed, clock=clock).run(PLAN)
    assert resumed.launches == []


def test_existing_prevents_launch(tmp_path):
    api = Backend()
    api.existing = INSTANCE
    engine(tmp_path, api).run(PLAN)
    assert api.launches == []
    assert api.queries == []


def test_notification_failure_cannot_launch_twice(tmp_path):
    def fail(_):
        raise RuntimeError("telegram offline")

    with pytest.raises(RuntimeError):
        engine(tmp_path, Backend([INSTANCE]), notify=fail).run(PLAN)
    api = Backend()
    messages = []
    engine(tmp_path, api, notify=messages.append).run(PLAN)
    assert len(messages) == 1
    assert api.launches == []


def test_changed_plan_refused(tmp_path):
    engine(tmp_path, Backend([INSTANCE])).run(PLAN)
    with pytest.raises(FatalError, match="plan differs"):
        engine(tmp_path, Backend()).run({**PLAN, "image_id": "new-image"})


def test_backoff_retry_after_and_max_attempts(tmp_path):
    api = Backend([service(429, headers={"Retry-After": "1000"}), service(503)])
    clock = Clock()
    result = engine(tmp_path, api, replace(Config(), max_attempts=2), clock).run(PLAN)
    assert result is None
    assert clock.waits == [1000]
    assert Store(tmp_path).load()["pending"]


def test_fatal_quota_stops(tmp_path):
    api = Backend([service(400, "LimitExceeded")])
    with pytest.raises(FatalError, match="LimitExceeded"):
        engine(tmp_path, api).run(PLAN)
    assert len(api.launches) == 1


def test_retry_after_date():
    assert retry_after(service(headers={"retry-after": "Thu, 01 Jan 1970 00:01:00 GMT"}), 0) == 60
    assert retry_after(service(headers={"Retry-After": "bad"}), 0) == 0


def test_lock(tmp_path):
    store = Store(tmp_path)
    import portalocker

    with store.lock(), pytest.raises(portalocker.exceptions.LockException), store.lock():
        pass


def test_corrupt_journal_stops(tmp_path):
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        engine(tmp_path, Backend()).run(PLAN)
