#!/usr/bin/env python3
"""Validate the 0.75 tab-grouping threshold (N9) against real
nomic-embed-text output.

Run on a machine with Ollama + nomic-embed-text installed
(install.sh pulls it — task I1):

    python3 tests/validate_threshold.py

Embeds a fixed set of same-topic and different-topic title pairs
and reports whether 0.75 separates them. Exits 0 when it does,
1 when it doesn't (or when no embeddings model answers) — the
exit code is the verdict, the printed ranges are the evidence.
Not a unittest: `discover` never picks it up (no test_ prefix).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from forge.browser.retrieve import embed_texts  # noqa: E402
from forge.browser.titles import GROUP_SIMILARITY, threshold_report  # noqa: E402


def main():
    try:
        report = threshold_report(embed_texts)
    except Exception as exc:
        print(f"no embeddings model answered: {exc}")
        return 1
    same, different = report["same"], report["different"]
    print(f"threshold: {GROUP_SIMILARITY}")
    print(f"same-topic pairs:     min {same['min']:.3f}  "
          f"max {same['max']:.3f}  mean {same['mean']:.3f}")
    print(f"different-topic pairs: min {different['min']:.3f}  "
          f"max {different['max']:.3f}  mean {different['mean']:.3f}")
    if report["separated"]:
        print("verdict: 0.75 separates same-topic from different-topic pairs")
        return 0
    print("verdict: 0.75 does NOT separate the pairs — "
          "the threshold needs adjusting")
    return 1


if __name__ == "__main__":
    sys.exit(main())
