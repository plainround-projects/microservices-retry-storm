"""Send one request through the chain with its trace forced on and print the span tree."""

import asyncio

from tests.system import harness


async def main() -> None:
    await harness.wait_ready()
    trace_id, summary = await harness.forced_request()
    print(f"gateway answered {summary['status']} in {summary['seconds']} s; trace {trace_id}")
    print(harness.render_trace(await harness.fetch_trace(trace_id)))


if __name__ == "__main__":
    asyncio.run(main())
