"""--shard K/N: run one runner's share of the selected tests, so CI can split a job across runners.

The tests are CPU-bound and every vCPU of a runner is already busy with them (pytest-xdist), so a job gets faster only
on more runners. CI runs the tests marked cluster this way, on two (.github/workflows/ci.yml).
"""

import pytest


def pytest_addoption(parser):
    parser.addoption("--shard", metavar="K/N", help="run only the K-th of every N selected tests, in collection order")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """After -m and -k have selected, keep the K-th of every N tests. Every runner collects the same tests in the same
    order, so the shares never overlap and together hold every test. Taking every N-th in file order splits the
    cluster tests about evenly, their long tests being spread through the file, with no durations file to keep."""
    shard = config.getoption("shard")
    if not shard:
        return
    try:
        k, n = (int(part) for part in shard.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard {shard}: expected K/N, such as 1/2") from None
    if not 1 <= k <= n:
        raise pytest.UsageError(f"--shard {shard}: K must be from 1 to N")
    keep = [item for i, item in enumerate(items) if i % n == k - 1]
    config.hook.pytest_deselected(items=[item for i, item in enumerate(items) if i % n != k - 1])
    items[:] = keep
