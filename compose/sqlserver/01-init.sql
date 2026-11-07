-- SQL Server lab: a read-only monitoring login, and an app database with some data. Run with sqlcmd, passing
-- -v DBTRIAGE_PASSWORD="..." (the k8s Job in k8s.yaml does). Safe to run again: each step checks first.
IF SUSER_ID('dbtriage') IS NULL
    CREATE LOGIN dbtriage WITH PASSWORD = '$(DBTRIAGE_PASSWORD)', CHECK_POLICY = ON;
GO
-- DMVs (query stats, wait stats, sessions, requests, counters). SQL Server 2022 has the narrower
-- VIEW SERVER PERFORMANCE STATE, which is all dbtriage needs; earlier versions only have VIEW SERVER STATE.
IF CAST(SERVERPROPERTY('ProductMajorVersion') AS int) >= 16
    EXEC ('GRANT VIEW SERVER PERFORMANCE STATE TO dbtriage');
ELSE
    EXEC ('GRANT VIEW SERVER STATE TO dbtriage');
GO
IF DB_ID('app') IS NULL CREATE DATABASE app;
GO
ALTER DATABASE app SET QUERY_STORE = ON (OPERATION_MODE = READ_WRITE);
GO
USE app;
IF USER_ID('dbtriage') IS NULL CREATE USER dbtriage FOR LOGIN dbtriage;
GRANT VIEW DATABASE STATE TO dbtriage;  -- index usage, Query Store, file stats in this database
GRANT VIEW DEFINITION TO dbtriage;      -- table and index names (metadata only: it cannot read a row)
GO
IF OBJECT_ID('dbo.orders') IS NULL
BEGIN
    CREATE TABLE dbo.orders (
        id BIGINT IDENTITY PRIMARY KEY, customer_id INT NOT NULL, status VARCHAR(16) NOT NULL,
        amount DECIMAL(10,2) NOT NULL, created_at DATETIME2 NOT NULL, note VARCHAR(64));
    CREATE INDEX orders_cust ON dbo.orders (customer_id);
END
GO
-- filled separately, so a run that stopped after creating the table fills it next time. Values come from a row
-- number: CHOOSE over NEWID() is a CASE that re-evaluates NEWID() per branch and can produce NULL.
IF NOT EXISTS (SELECT 1 FROM dbo.orders)
    INSERT INTO dbo.orders (customer_id, status, amount, created_at, note)
    SELECT i % 5000, CHOOSE(1 + i % 4, 'new', 'paid', 'shipped', 'cancelled'), (i * 37 % 50000) / 100.0,
           DATEADD(day, -(i % 90), SYSUTCDATETIME()), CONVERT(VARCHAR(64), NEWID())
    FROM (SELECT TOP (100000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS i
          FROM sys.all_objects a CROSS JOIN sys.all_objects b) AS n;
GO
