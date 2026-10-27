"""Run the lesson's experiments: python scripts/experiment.py <name>. Needs the stack (make up)."""

import asyncio
import sys

from tests.system import experiments as e

NAMES = {
    "amplification-naive": lambda: e.amplification("naive"),
    "slowdown-naive": lambda: e.slowdown("naive"),
    "amplification-policy": lambda: e.amplification("deadline-retry"),
    "slowdown-policy": lambda: e.slowdown("deadline-retry"),
    "outage": e.outage,
    "shedding": e.shedding,
    "slowdown-full": lambda: e.slowdown("full"),
}


async def main(names: list[str]) -> None:
    for name in names:
        print(f"\n===== {name}")
        await NAMES[name]()


if __name__ == "__main__":
    chosen = sys.argv[1:] or ["--help"]
    if chosen == ["all"]:
        chosen = list(NAMES)
    if any(n not in NAMES for n in chosen):
        sys.exit(f"usage: experiment.py {{{'|'.join(NAMES)}|all}}")
    asyncio.run(main(chosen))
