"""Send one request through the chain with its trace forced on and print the span tree."""

import asyncio

from tests.system import harness as h


async def main() -> None:
    await h.wait_ready()
    trace_id, summary = await h.forced_request()
    print(f"gateway answered {summary['status']} in {summary['seconds']} s; trace {trace_id}")
    print(h.render_trace(await h.fetch_trace(trace_id)))


if __name__ == "__main__":
    asyncio.run(main())
