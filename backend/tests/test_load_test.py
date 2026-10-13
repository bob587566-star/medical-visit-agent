import asyncio

import pytest

from scripts.load_test import percentile, run


def test_finite_probe_rejects_remote_and_oversized_scenarios_before_http():
    for url, kwargs in (("https://real.example", {}), ("http://127.0.0.1:8272", {"users": 100}),
                        ("http://localhost:8272", {"concurrency": 20})):
        with pytest.raises(ValueError):
            asyncio.run(run(url, **kwargs))


def test_nearest_rank_percentiles_are_reported_for_finite_samples():
    assert percentile([9, 1, 5, 2, 3], .5) == 3
    assert percentile([9, 1, 5, 2, 3], .95) == 9
