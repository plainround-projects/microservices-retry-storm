-- DDL for the stock store's own database. Run as the database owner; the app password arrives as the psql
-- variable app_password.

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
