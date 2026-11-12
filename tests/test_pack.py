from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langwars_stress.accounts import Account, AccountStore  # noqa: E402
from langwars_stress.metrics import Histogram  # noqa: E402
from langwars_stress.report import load_exit_code  # noqa: E402
from langwars_stress.solutions import DEFAULT_TASKS, TASK_SOLUTIONS, correct_source, timeout_source  # noqa: E402
from langwars_stress.variants import RESERVED_WORDS, CodeBook  # noqa: E402
from langwars_stress.workload import _outcome, select_tasks  # noqa: E402


class MetricTests(unittest.TestCase):
    def test_histogram_percentiles_and_reservoir(self):
        histogram = Histogram(capacity=10, seed=1)
        for value in range(100):
            histogram.add(float(value))
        snapshot = histogram.snapshot()
        self.assertEqual(snapshot["count"], 100)
        self.assertLessEqual(snapshot["p50Ms"], snapshot["p95Ms"])
        self.assertLessEqual(snapshot["p95Ms"], snapshot["p99Ms"])
        self.assertEqual(len(histogram.samples), 10)

    def test_outcome_unwraps_job_result(self):
        outcome = _outcome({
            "status": "completed",
            "result": {
                "status": "OK",
                "tests": [{"durationMs": 2.0}, {"durationMs": 3.0}],
            },
        }, 25.0, 3)
        self.assertEqual(outcome.state, "completed")
        self.assertEqual(outcome.verdict, "OK")
        self.assertEqual(outcome.execution_ms, 5.0)

    def test_outcome_reads_submission_and_rating(self):
        outcome = _outcome({
            "status": "completed",
            "result": {
                "submission": {"id": "sub_1", "status": "OK", "durationMs": 7.0},
                "ratingUpdate": {"delta": 50},
            },
        }, 30.0, 2)
        self.assertEqual(outcome.submission_id, "sub_1")
        self.assertEqual(outcome.rating_delta, 50.0)

    def test_thresholds(self):
        snapshot = {
            "requests": 1000, "transportErrors": 0, "serverErrors": 0, "clientErrors": 0,
            "ops": {"GET /x": {"codes": {"COOLDOWN": 5}}},
            "judge": {"completionRatio": 1.0},
        }
        code, problems = load_exit_code(snapshot, max_transport_rate=0.01, max_5xx_rate=0.005, min_job_completion=0.95, strict=False)
        self.assertEqual((code, problems), (0, []))
        snapshot["transportErrors"] = 100
        code, problems = load_exit_code(snapshot, max_transport_rate=0.01, max_5xx_rate=0.005, min_job_completion=0.95, strict=False)
        self.assertEqual(code, 1)
        self.assertTrue(problems)


class AccountStoreTests(unittest.TestCase):
    def test_round_trip_and_url_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "accounts.json"
            store = AccountStore(path, "https://example.org")
            store.save([Account("alice", "secret", "token", "usr_1")])
            loaded = AccountStore(path, "https://example.org").load()
            self.assertEqual(loaded[0].username, "alice")
            self.assertEqual(loaded[0].token, "token")
            with self.assertRaises(Exception):
                AccountStore(path, "https://other.example").load()


class VariantTests(unittest.TestCase):
    def code_book(self, group_size=4, players=8):
        tasks = [{"id": key, "slug": key} for key in TASK_SOLUTIONS]
        return CodeBook(
            tasks,
            allowed_words=list("abcdefghijklmnopqrstuvwxyz"),
            max_name_length=24,
            players=players,
            group_size=group_size,
        )

    def test_shadow_groups_share_names_but_rotate_them(self):
        book = self.code_book()
        first = book.player(0).task_maps["shared-sum"]
        second = book.player(1).task_maps["shared-sum"]
        other_group = book.player(4).task_maps["shared-sum"]
        self.assertNotEqual(first, second)
        self.assertTrue(set(first.values()) & set(second.values()))
        self.assertTrue(set(first.values()).isdisjoint(set(other_group.values())))

    def test_generated_names_are_valid_and_override_is_separate(self):
        book = self.code_book()
        player = book.player(0)
        generated = set()
        for mapping in player.task_maps.values():
            generated.update(mapping.values())
        for name in generated:
            self.assertRegex(name, r"^[A-Za-z]+(?:_[A-Za-z]+)*$")
            self.assertNotIn(name, RESERVED_WORDS)
            self.assertLessEqual(len(name), 24)
        task = {"id": "shared-sum", "slug": "shared-sum"}
        source = player.with_override(task, "eq")
        self.assertIn("@eq fn <int,int>", source)
        self.assertIn("__builtin_eq__", source)
        self.assertIn("@solution", source)
        function_names = {names[0] for names in player.override_names.values()}
        self.assertEqual(len(function_names), len(player.override_names))
        self.assertTrue(function_names.isdisjoint(generated))

    def test_group_of_one_has_no_shadowing(self):
        book = self.code_book(group_size=1)
        first = set(book.player(0).task_maps["shared-sum"].values())
        second = set(book.player(1).task_maps["shared-sum"].values())
        self.assertTrue(first.isdisjoint(second))

    def test_tiny_name_limit_is_reported(self):
        with self.assertRaises(ValueError):
            CodeBook(
                [{"id": "shared-sum", "slug": "shared-sum"}],
                allowed_words=list("abcdefghijklmnopqrstuvwxyz"),
                max_name_length=2,
                players=4,
            )


class SolutionTests(unittest.TestCase):
    def test_select_prefers_defaults_and_keeps_unknown(self):
        tasks = [{"id": "shared-sum", "slug": "shared-sum"}, {"id": "custom", "slug": "custom"}]
        selected = select_tasks(tasks, [])
        self.assertEqual([task["id"] for task in selected], ["shared-sum"])
        self.assertEqual([task["id"] for task in select_tasks(tasks, ["custom"])], ["custom"])
        self.assertEqual(select_tasks(tasks, ["missing"]), [])

    def test_default_reference_and_timeout(self):
        task = {"id": "shared-sum", "slug": "shared-sum", "entryPoint": "sum_answer"}
        self.assertIn("left+right", correct_source(task))
        self.assertIn("while(1<2)", timeout_source(task))
        self.assertIn("shared-sum", DEFAULT_TASKS)


if __name__ == "__main__":
    unittest.main()
