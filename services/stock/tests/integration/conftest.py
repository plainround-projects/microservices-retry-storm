import os

import pytest_asyncio

from chassis.postgres import create_pool


@pytest_asyncio.fixture
async def pool():
    password = os.environ["STOCK_APP_PASSWORD"]
    port = os.environ.get("STOCK_DB_PORT", "43432")
    pool = await create_pool(f"postgresql://stock_app:{password}@127.0.0.1:{port}/stock", name="it", wait_seconds=10)
    yield pool
    await pool.close()
