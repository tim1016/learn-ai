-- CREATE TABLE customers (
--   cust_id serial PRIMARY KEY,
--   cust_name VARCHAR(100) NOT NULL
-- );
-- CREATE TABLE orders (
--   ord_id serial PRIMARY KEY,
--   cust_id INT REFERENCES customers (cust_id),
--   ord_date TIMESTAMP DEFAULT current_timestamp
-- );
-- CREATE TABLE products (
--   prod_id serial PRIMARY KEY,
--   prod_name VARCHAR(100) NOT NULL,
--   prod_price DECIMAL(10, 2) NOT NULL
-- );
-- CREATE TABLE order_items (
--   ord_item_id serial PRIMARY KEY,
--   ord_id INT REFERENCES orders (ord_id),
--   prod_id INT REFERENCES products (prod_id),
--   quantity INT NOT NULL
-- );
-- INSERT INTO
--   customers (cust_name)
-- VALUES
--   ('Ariana Grande'),
--   ('Bruno Mars'),
--   ('Charlie Puth');
-- INSERT INTO products (prod_name, prod_price)
-- VALUES ('Laptop', 999.99),
--        ('Smartphone', 499.49),
--        ('Tablet', 299.29);
-- INSERT INTO orders (cust_id)
-- VALUES (1),
--        (2),
--        (3);
-- INSERT INTO order_items (ord_id, prod_id, quantity)
-- VALUES (1, 1, 1),
--        (1, 2, 2),
--        (2, 2, 1),
--        (3, 3, 3);
-- SELECT
--   *
-- FROM
--   orders;
-- SELECT
--   *
-- FROM
--   order_items;
-- SELECT
--   *
-- FROM
--   products;
-- SELECT
--   *
-- FROM
--   customers;
SELECT
  c.cust_name,
  o.ord_id,
  oi.quantity,
  p.prod_name,
  p.prod_price,
  o.ord_date
FROM
  customers c
  LEFT JOIN orders o ON c.cust_id = o.cust_id
  LEFT JOIN order_items oi ON o.ord_id = oi.ord_id
  LEFT JOIN products p ON oi.prod_id = p.prod_id;
