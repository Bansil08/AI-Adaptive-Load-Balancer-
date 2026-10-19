# Concepts map

Every feature in this project, the file that implements it, and the CS concept it demonstrates.

| Feature | File | Subject | Concept | Interview one-liner |
|---------|------|---------|---------|---------------------|
| Normalised schema | `db/schema.sql` | DBMS | Third normal form (3NF) | Every non-key column depends only on its table's key, so facts like a server's address are stored once and can't drift out of sync. |
| Foreign keys + `ON DELETE` rules | `db/schema.sql` | DBMS | Referential integrity | FKs stop orphan rows; servers use RESTRICT + soft delete to keep history, run results CASCADE with their run. |
| CHECK / NOT NULL constraints | `db/schema.sql` | DBMS | Domain integrity | The database itself rejects impossible data (CPU 150%, check_type 'udp'), not just the application. |
| Atomic schema migration | `scripts/init_db.py` | DBMS | Transactions (transactional DDL) | Postgres runs CREATE TABLE inside a transaction, so the schema is applied entirely or not at all. |
| Stored run aggregates | `db/schema.sql` (`run_results`) | DBMS / System design | Controlled denormalisation | p95 etc. are derivable from `requests` but cached so plots are cheap and results outlive the 7-day retention job. |
