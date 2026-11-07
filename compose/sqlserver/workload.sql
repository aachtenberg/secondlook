-- A little lab load: a scan-heavy query and a point update, a few hundred times. Run as a login that can read
-- and write app (the lab's sa): sqlcmd -C -S localhost -U sa -P ... -d app -i workload.sql
SET NOCOUNT ON;
DECLARE @i INT = 1, @n INT;
WHILE @i <= 300
BEGIN
    SELECT @n = COUNT(*) FROM dbo.orders WHERE note LIKE '%ab%' AND amount > @i % 400;
    UPDATE dbo.orders SET amount = amount WHERE id = @i;
    SELECT TOP (5) @n = customer_id FROM dbo.orders WHERE customer_id = @i % 5000 ORDER BY created_at DESC;
    SET @i += 1;
END
