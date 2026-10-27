import pytest
from psycopg import errors

from stock.infrastructure.postgres import PostgresStockRepository

pytestmark = pytest.mark.integration


async def test_the_seeded_skus_have_stock_and_an_unknown_sku_has_none(pool):
    repository = PostgresStockRepository(pool)
    assert await repository.quantity("sku-001") == 23
    assert await repository.quantity("sku-005") == 6
    assert await repository.quantity("sku-404") is None


async def test_the_application_role_can_read_but_not_write(pool):
    async with pool.connection() as connection:
        with pytest.raises(errors.InsufficientPrivilege):
            await connection.execute("UPDATE stock_levels SET quantity = 0")
