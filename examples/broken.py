"""Demo module for testing the self-healing system.

This file ships INTENTIONALLY buggy: divide(x, 0) raises ZeroDivisionError.

Try the full self-heal demo:
    mytool crash-test --demo        # tool crashes, crash gets logged, AI fixes it
    mytool crashes                  # see the recorded crash
    git checkout examples/broken.py # restore the bug to demo again
"""
from __future__ import annotations


def divide(a: float, b: float) -> float:
    return 1 / b


if __name__ == "__main__":
    import sys

    if "--heal-verify" in sys.argv:
        # self-check used by the healer's verification step
        assert abs(divide(6, 3) - 2.0) < 1e-9
        assert divide(5, 0) == float("inf")
        print("heal-verify OK")
    else:
        print("divide(6, 3) =", divide(6, 3))
