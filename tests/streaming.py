#!/usr/bin/env python3
"""Regression for inference exceeding the former five-minute total deadline."""
import time

import check


def main():
    case = check.Checks()
    case.setUp()
    case.server.mode = "slow"
    case.server.delay = 302
    process = check.Process(case)
    try:
        started = time.monotonic()
        process.send("WAIT")
        process.wait("OK WAIT", timeout=325)
        elapsed = time.monotonic() - started
        assert elapsed > 300, elapsed
        assert len(case.server.requests) == 1, len(case.server.requests)
        assert not [r for r in case.records() if r["type"] == "request_error"]
        print("Long streaming inference verified without retries.")
    finally:
        process.finish()
        case.tearDown()


if __name__ == "__main__":
    main()
