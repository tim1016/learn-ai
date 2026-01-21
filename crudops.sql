DROP TABLE IF EXISTS users;


CREATE TABLE users (id SERIAL PRIMARY KEY,
                              name VARCHAR(100) NOT NULL,
                                                email VARCHAR(100) UNIQUE NOT NULL);


INSERT INTO users (name, email)
VALUES ('Alice', 'a@b.com'),
       ('Bob', 'b@b.com'),
       ('Charlie', 'c@b.com');


DELETE
FROM users
WHERE name = 'Bob';


select *
from users;