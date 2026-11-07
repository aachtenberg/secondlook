create extension if not exists pg_stat_statements;

-- read-only collector role: pg_monitor makes pg_stat_statements query text visible
create role dbtriage login password 'dbtriage';
grant pg_monitor to dbtriage;
grant connect on database app to dbtriage;

-- sample schema for the load generator
create table orders (
  id bigserial primary key, customer_id int not null, status text not null,
  amount numeric(10,2) not null, created_at timestamptz not null default now(), note text
);
create index on orders (customer_id);
insert into orders (customer_id, status, amount, created_at, note)
select (random()*5000)::int, (array['new','paid','shipped','cancelled'])[1+(random()*3)::int],
       (random()*500)::numeric(10,2), now() - (random()*90||' days')::interval, md5(random()::text)
from generate_series(1, 300000);
analyze orders;
