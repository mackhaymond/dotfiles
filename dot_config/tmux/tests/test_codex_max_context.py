import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "codex_max_context", Path(__file__).resolve().parents[1] / "scripts/codex-max-context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MaximumContextTests(unittest.TestCase):
    def test_request_allows_switching_to_larger_models(self):
        catalog = {"models": [
            {"slug": "small", "context_window": 272000, "max_context_window": 272000},
            {"slug": "large", "context_window": 272000, "max_context_window": 872000},
        ]}
        self.assertEqual(module.maximum_request(catalog), 872000)
        self.assertEqual(catalog["models"][1]["context_window"], 272000)

    def test_does_not_invent_a_limit_when_metadata_is_missing(self):
        for maxima in ([], [None], [-1, 0, True, "1000000"]):
            with self.subTest(maxima=maxima), self.assertRaises(ValueError):
                module.maximum_request({"models": [{"max_context_window": value} for value in maxima]})
        for catalog in (None, [], {}, {"models": None}, {"models": [None]}):
            with self.subTest(catalog=catalog), self.assertRaises(ValueError):
                module.maximum_request(catalog)

    def test_native_refresh_preserves_user_options_and_bounds_startup(self):
        catalog = {"models": [{"max_context_window": 872000}]}
        original = ["-m", "small", "-c", "model_context_window=128000", "resume", "--last"]
        with mock.patch.object(module.subprocess, "check_output", return_value=json.dumps(catalog)) as refresh:
            result = module.launch_args("/native/codex", original)
        self.assertEqual(result, ["-c", "model_context_window=872000"] + original)
        self.assertEqual(original[-2:], ["resume", "--last"])
        refresh.assert_called_once_with(["/native/codex", "debug", "models"], text=True,
                                        stderr=subprocess.DEVNULL, timeout=8)


if __name__ == "__main__":
    unittest.main()
