"""The Statistics view's distribution of retrieval times."""

import pytest

from scrapemm.server.retrieval_stats import _time_histogram

pytestmark = pytest.mark.server


def test_retrieval_times_fall_into_5_second_bins_and_one_beyond_a_minute():
    bins = _time_histogram([0.0, 4.99, 5.0, 12.3, 59.9, 60.0, 245.0])
    assert len(bins) == 13
    assert (bins[0]["from"], bins[0]["to"]) == (0, 5)
    assert (bins[-1]["from"], bins[-1]["to"]) == (60, None)
    counts = [b["count"] for b in bins]
    assert counts[0] == 2  # 0 and 4.99
    assert counts[1] == 1  # 5.0 starts the next bin
    assert counts[2] == 1  # 12.3
    assert counts[11] == 1  # 59.9
    assert counts[12] == 2  # 60 and 245: a minute or more
    assert sum(counts) == 7


def test_no_retrievals_give_empty_bins():
    assert [b["count"] for b in _time_histogram([])] == [0] * 13
