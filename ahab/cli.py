import argparse
import json
import logging
import os
import signal

import oci
import portalocker

from .config import Config
from .engine import Engine, FatalError, classify
from .notify import send, validate_environment
from .oracle import Oracle
from .storage import Store

LOG = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Ahab - OCI A1 hunter")
    parser.add_argument("command", choices=("discover", "doctor", "run"))
    parser.add_argument("--config", default="ahab.toml", help="TOML file (default: ahab.toml)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Read-only checks; no resources created"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.name == "posix":
        os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        config = Config.load(args.config)
        validate_environment()
        api = Oracle(config)
        if args.command == "discover":
            print(json.dumps(api.discover(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "doctor" or args.dry_run:
            plan = api.prepare()
            # Omit the public key from output.
            print(json.dumps({k: v for k, v in plan.items() if k != "ssh_public_key"}, indent=2))
            existing = api.find_existing()
            LOG.info("Checks passed. Existing match: %s", existing.id if existing else "none")
            LOG.info("Read-only: no capacity report requested or resources created")
            return 0
        store = Store(config.state_dir)
        with store.lock():
            saved = store.load()
            plan = api.prepare(saved.get("plan"))
            result = Engine(config, api, store, notify=send).run(plan)
            if result:
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0
            return 2
    except KeyboardInterrupt:
        LOG.info("Stopped; journal preserved for safe resumption")
        return 130
    except portalocker.exceptions.LockException:
        LOG.error("Another process is using this state_dir")
        return 1
    except oci.exceptions.ServiceError as exc:
        LOG.error(
            "OCI HTTP %s %s (request-id: %s)",
            exc.status,
            exc.code,
            getattr(exc, "request_id", "unavailable"),
        )
        return 75 if classify(exc) != "fatal" else 1
    except Exception as exc:
        if classify(exc) != "fatal":
            LOG.error("Transient connection failure; retry with the existing journal")
            return 75
        # Avoid exposing credentials in tracebacks.
        if isinstance(exc, (FatalError, ValueError, OSError, RuntimeError)):
            LOG.error("%s", exc)
        else:
            LOG.error("Failure: %s; check configuration and state", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
