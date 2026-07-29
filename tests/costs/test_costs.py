from __future__ import annotations

import pytest

from radar.costs import estimate_batch_cost


class TestEstimateBatchCost:
    def test_total_is_the_sum_of_anthropic_and_elevenlabs(self):
        estimate = estimate_batch_cost(3)

        assert estimate["total_usd"] == round(estimate["anthropic_usd"] + estimate["elevenlabs_usd"], 3)

    def test_more_videos_costs_more(self):
        small = estimate_batch_cost(1)
        large = estimate_batch_cost(5)

        assert large["total_usd"] > small["total_usd"]
        assert large["anthropic_usd"] > small["anthropic_usd"]
        assert large["elevenlabs_usd"] > small["elevenlabs_usd"]

    def test_elevenlabs_cost_is_proportional_to_count(self):
        one = estimate_batch_cost(1)
        four = estimate_batch_cost(4)

        assert four["elevenlabs_usd"] == pytest.approx(one["elevenlabs_usd"] * 4, abs=0.005)

    def test_unknown_model_falls_back_to_default_pricing(self):
        known = estimate_batch_cost(2, model="claude-sonnet-5")
        unknown = estimate_batch_cost(2, model="some-future-model")

        assert unknown["anthropic_usd"] == known["anthropic_usd"]
