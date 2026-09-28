"""Independent exact rational oracle for PRD #2540; imports no production code."""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

CASES = [
    (5, {"bot:c": "1", "bot:a": "1", "bot:b": "1"}),
    (11, {"bot:a": "0.125", "bot:b": "0.375", "manual:owner": "0.5"}),
    (1, {"a": "0.00000000000000000000001", "b": "0.00000000000000000000002"}),
    (123, {"a": "1.23789", "b": "8.98273", "c": "0"}),
]


def generate() -> None:
    output = []
    for cents, weights in CASES:
        population = sum(Fraction(weight) for weight in weights.values())
        quotas = {subject: cents * Fraction(weight) / population for subject, weight in weights.items()}
        answer = {subject: int(quota) for subject, quota in quotas.items()}
        fractions = sorted(quotas, key=lambda key: (quotas[key] % 1, tuple(-ord(char) for char in key)), reverse=True)
        for subject in fractions[:cents - sum(answer.values())]:
            answer[subject] += 1
        output.append({"cents": cents, "weights": weights, "expected_cents": answer})
    Path(__file__).with_name("cases.json").write_text(json.dumps(output, indent=2) + "\n")


if __name__ == "__main__":
    generate()
