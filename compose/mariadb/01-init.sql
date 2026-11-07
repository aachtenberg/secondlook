-- MariaDB lab: the MySQL collector reads it the same way (performance_schema must be ON; it is off by default)
CREATE USER 'dbtriage'@'%' IDENTIFIED BY 'dbtriage';
GRANT SELECT ON performance_schema.* TO 'dbtriage'@'%';
GRANT PROCESS, SELECT ON *.* TO 'dbtriage'@'%';

USE app;
CREATE TABLE orders (
  id BIGINT AUTO_INCREMENT PRIMARY KEY, customer_id INT NOT NULL, status VARCHAR(16) NOT NULL,
  amount DECIMAL(10,2) NOT NULL, created_at DATETIME NOT NULL, note VARCHAR(64), INDEX (customer_id)
);
SET SESSION max_recursive_iterations = 200000;
INSERT INTO orders (customer_id, status, amount, created_at, note)
WITH RECURSIVE seq AS (SELECT 1 n UNION ALL SELECT n+1 FROM seq WHERE n < 100000)
SELECT FLOOR(RAND()*5000), ELT(1+FLOOR(RAND()*4),'new','paid','shipped','cancelled'),
       ROUND(RAND()*500,2), NOW() - INTERVAL FLOOR(RAND()*90) DAY, MD5(RAND()) FROM seq;
