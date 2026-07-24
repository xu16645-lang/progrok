import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from performance_tuning import AdaptiveRegistrationTuner, machine_profile


class PerformanceTuningTests(unittest.TestCase):
    def test_healthy_window_keeps_configured_concurrency(self):
        tuner = AdaptiveRegistrationTuner(True, 6, 800)
        for _ in range(20):
            tuner.record(True)
        changed = tuner.evaluate(
            {"cpu_percent": 50, "memory_available_percent": 50},
            {"thread": 6, "in_flight": 2},
        )
        self.assertTrue(changed)
        self.assertEqual(tuner.current_concurrency, 6)
        self.assertEqual(tuner.stagger_ms, 700)

    def test_pressure_window_keeps_configured_concurrency(self):
        tuner = AdaptiveRegistrationTuner(True, 8, 600)
        for index in range(20):
            tuner.record(index >= 4, "captcha timeout" if index < 4 else "")
        tuner.evaluate({"cpu_percent": 60, "memory_available_percent": 45})
        self.assertEqual(tuner.current_concurrency, 8)
        self.assertEqual(tuner.stagger_ms, 1000)

    def test_manual_mode_keeps_configured_values(self):
        tuner = AdaptiveRegistrationTuner(False, 5, 1200)
        self.assertEqual(tuner.current_concurrency, 5)
        self.assertEqual(tuner.stagger_ms, 1200)

    @patch(
        "performance_tuning.system_sample",
        return_value={
            "physical_cores": 8,
            "logical_cores": 16,
            "memory_available_gb": 18,
        },
    )
    def test_machine_profile_respects_solver_and_global_caps(self, _sample):
        profile = machine_profile(
            provider="local", solver_threads=6, local_slots=6, global_limit=8
        )
        self.assertEqual(profile["recommended_concurrency"], 6)
        self.assertEqual(profile["effective_cap"], 6)


if __name__ == "__main__":
    unittest.main()
