"""Tests for scripts/count_tests.py (#3357).

Stdlib-only (unittest), like ``scripts/test_alpaca_onboarding_gates.py``. The
counter is the receipt for the test-cut project (#3356), so what it counts as a
function and as an assertion is pinned here: a parametrized test counts once,
closures, comments and strings never count, and a file that cannot be parsed
stops the run instead of shrinking the numbers.

Run directly: ``python3 scripts/test_count_tests.py``
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from count_tests import (
    CountError,
    Counts,
    blank_ts_noncode,
    count_python_source,
    count_typescript_source,
    main,
)


def _py(source: str, *, is_test_module: bool = True) -> Counts:
    return count_python_source(textwrap.dedent(source), is_test_module=is_test_module, path="t.py")


def _ts(source: str) -> Counts:
    return count_typescript_source(textwrap.dedent(source))


class PythonCountingTests(unittest.TestCase):
    def test_parametrized_test_counts_once(self) -> None:
        counts = _py(
            """
            import pytest

            @pytest.mark.parametrize("x", [1, 2, 3, 4])
            def test_square(x):
                assert x * x >= x
            """
        )

        self.assertEqual(counts.functions, 1)

    def test_async_methods_and_unprefixed_names_count_closures_do_not(self) -> None:
        counts = _py(
            """
            async def test_async():
                def test_closure():
                    pass

            class TestThing:
                def test_method(self):
                    pass

                def helper(self):
                    pass

            def testpolygon_without_underscore():
                pass

            def helper_test():
                pass
            """
        )

        # test_async, TestThing.test_method, testpolygon_without_underscore:
        # pytest collects `test*` names, so the missing underscore still counts.
        self.assertEqual(counts.functions, 3)

    def test_comments_and_strings_never_count(self) -> None:
        counts = _py(
            '''
            # def test_commented_out(): assert True
            DOC = """
            def test_in_a_string():
                assert False
            """
            '''
        )

        self.assertEqual((counts.functions, counts.assertions), (0, 0))

    def test_non_test_module_adds_lines_and_assertions_but_no_functions(self) -> None:
        counts = _py("def test_helper_not_collected():\n    assert True\n", is_test_module=False)

        self.assertEqual((counts.functions, counts.lines, counts.assertions), (0, 2, 1))

    def test_assertion_kinds(self) -> None:
        counts = _py(
            """
            import numpy as np
            import pytest

            def test_all_kinds(mock, case):
                assert 1 == 1
                with pytest.raises(ValueError):
                    int("x")
                with pytest.warns(UserWarning):
                    pass
                np.testing.assert_allclose(1, 1, atol=0, rtol=0)
                mock.assert_called_once_with(1)
                case.assertEqual(1, 1)
                assert_frame_equal(1, 1)
                not_an_assertion(1)
                case.assertive_neighbour()
            """
        )

        # assert, raises, warns, assert_allclose, assert_called_once_with,
        # assertEqual, assert_frame_equal. `assertive_neighbour` is not one.
        self.assertEqual(counts.assertions, 7)

    def test_syntax_error_names_the_file(self) -> None:
        with self.assertRaisesRegex(CountError, "t.py: cannot parse"):
            _py("def test_broken(:\n    pass\n")


class TypeScriptCountingTests(unittest.TestCase):
    def test_test_call_forms_count_once_each(self) -> None:
        counts = _ts(
            """
            describe('suite', () => {
              it('plain', () => {});
              test('alias', () => {});
              it.each([[1], [2]])('table %s', (n) => {});
              test.each([[1], [2]])('table %s', (n) => {});
              it.each<[number]>([[1]])('typed %s', (n) => {});
              it.each`
                a | b
                ${1} | ${2}
              `('tagged $a', ({ a }) => {});
              it.for([1, 2])('for %s', (n) => {});
              test.for`
                a
                ${1}
              `('for tagged $a', ({ a }) => {});
              it.skip('skipped', () => {});
              it.only('only', () => {});
            });
            """
        )

        self.assertEqual(counts.functions, 10)

    def test_comments_strings_and_member_calls_do_not_count(self) -> None:
        counts = _ts(
            """
            // it('commented out', () => { expect(1).toBe(1); });
            /* test('block comment', () => { expect(2).toBe(2); }); */
            const name = "it('in a string', () => expect(3).toBe(3))";
            const same = `expect(${helper('test(x)')}).toBe(4)`;
            const ok = /^a+$/.test(value);
            const fit = chart.fit(1);
            const latest = contest(1);
            """
        )

        self.assertEqual((counts.functions, counts.assertions), (0, 0))

    def test_assertions_count_expect_and_expect_soft_only(self) -> None:
        counts = _ts(
            """
            it('asserts', () => {
              expect(a).toBe(1);
              expect.soft(b).toBe(2);
              expect(c).toEqual(expect.objectContaining({ x: 1 }));
              expect.assertions(3);
              expectTypeOf(d).toBeString();
              const dyn = `${expect(e).toBe(5)}`;
            });
            """
        )

        # expect(a), expect.soft(b), expect(c), expect(e) inside a template.
        self.assertEqual(counts.assertions, 4)

    def test_quote_inside_regex_literal_does_not_swallow_later_tests(self) -> None:
        counts = _ts(
            """
            const stripped = text.replace(/['"]/g, ''); it('same line', () => { expect(stripped).toBe('x'); });
            const half = total / 2; const other = half / 2; it('after a division', () => { expect(half).toBe(1); });
            """
        )

        self.assertEqual((counts.functions, counts.assertions), (2, 2))

    def test_blanking_keeps_line_numbers(self) -> None:
        source = "// a\n/* b\nc */\nconst s = 'x\\'y';\nconst t = `a\nb ${1 + 1}`;\n"

        self.assertEqual(blank_ts_noncode(source).count("\n"), source.count("\n"))


class SuiteWalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _write(self, relative: str, text: str) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")

    def _layout(self) -> None:
        self._write("PythonDataService/tests/broker/alpaca/test_a.py", "def test_a():\n    assert 1\n")
        self._write(
            "PythonDataService/tests/broker/test_b.py", "def test_b1():\n    pass\n\ndef test_b2():\n    pass\n"
        )
        self._write("PythonDataService/tests/broker/conftest.py", "VALUE = 1\n")
        self._write("PythonDataService/tests/__pycache__/test_skipped.py", "def test_skipped():\n    pass\n")
        self._write("PythonDataService/app/engine/tests/test_e.py", "def test_e():\n    pass\n")
        self._write("PythonDataService/app/engine/strategy/spec/tests/test_s.py", "def test_s():\n    pass\n")
        self._write("Frontend/src/app/components/bots/bots.spec.ts", "it('a', () => { expect(1).toBe(1); });\n")
        self._write("Frontend/src/app/components/bots/deep/more.spec.ts", "it('b', () => {});\n")
        self._write("Frontend/src/app/components/bots/helper.ts", "it('not a spec', () => {});\n")

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--repo-root", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_json_groups_by_folder_and_totals(self) -> None:
        self._layout()

        code, out, _ = self._run("--json", "--depth", "2")

        self.assertEqual(code, 0)
        report = json.loads(out)
        # conftest.py adds lines but no functions; the subfolder is its own row.
        self.assertEqual(
            report["python"]["folders"]["tests/broker"],
            {"files": 2, "functions": 2, "lines": 6, "assertions": 0},
        )
        self.assertEqual(
            report["python"]["folders"]["tests/broker/alpaca"],
            {"files": 1, "functions": 1, "lines": 2, "assertions": 1},
        )
        self.assertEqual(report["python"]["total"]["functions"], 5)
        self.assertEqual(report["frontend"]["total"], {"files": 2, "functions": 2, "lines": 2, "assertions": 1})
        self.assertEqual(report["total"]["functions"], 7)
        self.assertNotIn("tests/__pycache__", report["python"]["folders"])

    def test_depth_changes_folder_grouping(self) -> None:
        self._layout()

        _, shallow, _ = self._run("--json", "--suite", "frontend", "--depth", "2")
        _, deep, _ = self._run("--json", "--suite", "frontend", "--depth", "4")

        self.assertEqual(list(json.loads(shallow)["frontend"]["folders"]), ["src/app/components"])
        self.assertEqual(
            list(json.loads(deep)["frontend"]["folders"]),
            ["src/app/components/bots", "src/app/components/bots/deep"],
        )

    def test_markdown_table_has_a_total_row(self) -> None:
        self._layout()

        code, out, _ = self._run()

        self.assertEqual(code, 0)
        # Default depth for Python is 1, so the alpaca subfolder folds into broker.
        self.assertIn("| `tests/broker` | 3 | 3 | 8 | 1 |", out)
        self.assertIn("| **Total** | 7 | 7 | 14 | 2 |", out)

    def test_unparseable_file_fails_loudly_with_its_path(self) -> None:
        self._layout()
        self._write("PythonDataService/tests/test_bad.py", "def test_bad(:\n")

        code, out, err = self._run()

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("test_bad.py: cannot parse", err)

    def test_missing_root_fails_loudly(self) -> None:
        code, _, err = self._run("--suite", "frontend")

        self.assertEqual(code, 2)
        self.assertIn("test root does not exist", err)


if __name__ == "__main__":
    unittest.main()
