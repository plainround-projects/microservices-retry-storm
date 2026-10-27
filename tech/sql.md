# SQL

> New to the words used here (process, port, thread, environment variable, JSON, transaction and the like)? Read [the list of everyday words](README.md#words-you-will-meet-in-every-note) first.

## What it is

SQL (Structured Query Language) is the language used to talk to a relational database. A **relational database** stores data in **tables**. A table has **columns** (the named fields, each with a type) and **rows** (one record each). You write statements; the database runs them and answers.

There are three kinds of statements. **DDL** (data definition) creates or changes the shape: `CREATE TABLE`, `DROP TABLE`. **DML** (data manipulation) changes the data: `INSERT`, `UPDATE`, `DELETE`. **Queries** read: `SELECT`. Another kind, `GRANT` and `REVOKE`, controls who may do what.

## Why this repository uses it

The bottom of the chain is a stock store with a real database behind it, so the slowdown the lesson studies is the slowdown of something that really reads rows. The data is small and fixed: twenty SKUs. The SQL files make it reproducible, and the small queries show which SKUs are low in stock, which the pricing service turns into a surcharge.

## Where it appears

| Path | What it holds |
| --- | --- |
| `services/stock/sql/001_schema.sql` | DDL: one table, and the application role with read-only rights |
| `services/stock/sql/002_seed.sql` | DML: the twenty SKUs |
| `services/stock/sql/003_queries.sql` | Two read-only checks |
| `services/stock/src/stock/infrastructure/postgres.py` | The one query the service runs |

## How it works underneath

You send text to the database server. The server parses it, plans the cheapest way to do the work, runs it, and sends back rows or a count of changed rows. A statement ends with `;`.

- `SELECT columns FROM table WHERE condition ORDER BY column` picks rows. `GROUP BY` collapses rows into groups so you can count or sum each group. `HAVING` filters the groups. `JOIN ... ON` combines rows from two tables where the condition is true.
- **Constraints** are rules the database enforces on every write: `NOT NULL` (a value is required), `PRIMARY KEY` (unique and required, identifies the row), `UNIQUE` (no two rows alike), `CHECK (condition)` (a custom rule), `REFERENCES other(id)` (a foreign key: the value must exist in the other table), `DEFAULT value` (used when none is given).
- A **transaction** groups statements so they all happen or none do. A single statement is always its own transaction.
- **Placeholders.** Programs never paste values into SQL text. They send the SQL with `%s` (or `$1`) markers and send the values separately, so a value can never be mistaken for SQL. This prevents SQL injection.

## Walkthrough of this repository's use

### The schema

```sql
SELECT format('CREATE ROLE stock_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'stock_app') \gexec
SELECT format('ALTER ROLE stock_app PASSWORD %L', :'app_password') \gexec

CREATE TABLE IF NOT EXISTS stock_levels (
    sku      text    PRIMARY KEY CHECK (sku <> ''),
    quantity integer NOT NULL CHECK (quantity >= 0)
);
COMMENT ON TABLE stock_levels IS 'Units on hand per sku. Read by every stock check; written by nothing in this project.';

-- Least privilege: the service only reads.
GRANT SELECT ON stock_levels TO stock_app;
```

- `SELECT format('CREATE ROLE stock_app LOGIN PASSWORD %L', :'app_password') WHERE NOT EXISTS (...) \gexec`: `CREATE ROLE` has no `IF NOT EXISTS`, so the statement is built as text only when the role is missing, and `\gexec` (a `psql` command) runs each result row as SQL. `:'app_password'` is a `psql` variable passed by `scripts/apply-sql.sh` with `-v app_password=...`; `%L` quotes it safely as a literal. The second `SELECT ... \gexec` resets the password on every run.
- `sku text PRIMARY KEY CHECK (sku <> '')`: the id is unique and not empty. `quantity integer NOT NULL CHECK (quantity >= 0)`: a count that cannot go negative.
- `COMMENT ON TABLE ... IS '...'` stores a note in the database.
- `GRANT SELECT ON stock_levels TO stock_app;` is the whole of the application role's rights: it can only read (least privilege). Nothing in this project writes stock.

### The seed

```sql
-- DML: the 20 skus of the catalog (sku-001 to sku-020). Four of them have fewer than ten units, which pricing
-- turns into a 15 percent surcharge. Safe to run again.
INSERT INTO stock_levels (sku, quantity)
SELECT format('sku-%s', lpad(n::text, 3, '0')), CASE WHEN n % 5 = 0 THEN n % 7 + 1 ELSE 20 + n * 3 END
FROM generate_series(1, 20) AS n
ON CONFLICT (sku) DO NOTHING;
```

`generate_series(1, 20) AS n` produces the numbers 1 to 20 as rows. `format('sku-%s', lpad(n::text, 3, '0'))` builds `sku-001`: `n::text` converts to text, `lpad(text, 3, '0')` pads on the left with zeros. `CASE WHEN n % 5 = 0 THEN n % 7 + 1 ELSE 20 + n * 3 END` chooses a quantity: every fifth SKU gets a small number (`n % 5` is the remainder after dividing by 5), the rest get a larger one. So SKUs 5, 10, 15 and 20 are low. `ON CONFLICT (sku) DO NOTHING` skips rows that already exist, so the seed can run twice.

### The checks

```sql
-- The queries used while measuring. Nothing here changes data.

-- Every sku and its stock; the skus under ten units are the ones priced with a surcharge.
SELECT sku, quantity, quantity < 10 AS low_stock FROM stock_levels ORDER BY sku;

-- How many skus are low.
SELECT count(*) FILTER (WHERE quantity < 10) AS low_stock_skus, count(*) AS skus FROM stock_levels;
```

`quantity < 10 AS low_stock` is a computed column of true/false. `count(*) FILTER (WHERE quantity < 10)` counts only the rows matching the condition, `count(*)` counts all. The result is `4` and `20`.

### SQL sent from Python

```python
    async def quantity(self, sku: str) -> int | None:
        """Units available, or ``None`` for an unknown sku."""
        async with self._pool.connection() as connection:
            cursor = await connection.execute("SELECT quantity FROM stock_levels WHERE sku = %s", (sku,))
            row = await cursor.fetchone()
        return None if row is None else int(row["quantity"])
```

The `%s` placeholder carries the sku separately from the SQL text, which prevents SQL injection. `await cursor.fetchone()` returns one row as a dictionary, or `None` when the SKU is unknown. See `psycopg.md` for the async pool.

### The header of the schema file

```sql
-- DDL for the stock store's own database. Run as the database owner; the app password arrives as the psql
-- variable app_password.
```

Two comment lines: the file is the stock store's own database, run as the database owner, and the application password arrives as the `psql` variable `app_password`.

## Try it yourself

These need a running PostgreSQL with the SQL applied (`make up`, then the form below).

1. Run the checks:

   ```bash
   make queries
   ```

   Expect a table of 20 rows (`sku-001` with 23, `sku-005` with 6 and `low_stock` true, `sku-010` with 4 and `low_stock` true) and then `low_stock_skus | skus` with `4 | 20`.

2. Try to break a rule. The database refuses a negative quantity:

   ```bash
   docker compose exec -T stock-db psql -U stock_owner -d stock -c "INSERT INTO stock_levels VALUES ('x', -1)"
   ```

   Expect `ERROR:  new row for relation "stock_levels" violates check constraint "stock_levels_quantity_check"`.

3. Try the application role's limits:

   ```bash
   set -a; . ./.env; set +a
   docker compose exec -T -e PGPASSWORD="$STOCK_APP_PASSWORD" stock-db psql -U stock_app -d stock -c "DELETE FROM stock_levels"
   ```

   Expect `ERROR:  permission denied for table stock_levels`.

## Mistakes people make

- **Gluing user input into SQL strings.** That is SQL injection. Use placeholders.
- **`UPDATE` or `DELETE` without `WHERE`.** It changes every row.
- **Comparing with `NULL` using `=`.** `x = NULL` is never true; write `x IS NULL`.
- **Using `float` for money.** Use `numeric`.
- **Trusting the application to check everything.** Put the important rules in constraints so no program can break them.
- **`SELECT *` in application code.** A later column change silently changes the program's input.

## Check yourself

1. What is the difference between DDL and DML?
   Answer: DDL changes the structure (tables, columns); DML changes the data (rows).
2. What does a `CHECK` constraint do?
   Answer: it makes the database reject any row that breaks the condition.
3. Why use `%s` placeholders instead of building the string?
   Answer: values are sent separately from the SQL, so they cannot change its meaning (no injection).
4. What does `GROUP BY` plus `HAVING count(*) > 1` find?
   Answer: groups that occur more than once, for example duplicates.
5. Why is `x = NULL` a mistake?
   Answer: comparing with NULL gives "unknown", never true; use `IS NULL`.

## Further reading

- PostgreSQL 18 SQL reference (the dialect this repository runs): https://www.postgresql.org/docs/18/sql.html
- The SQL language chapters (data definition, queries): https://www.postgresql.org/docs/18/ddl.html and https://www.postgresql.org/docs/18/queries.html
