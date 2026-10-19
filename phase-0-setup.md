Read PROJECT_SPEC.md fully before doing anything. We are starting Phase 0 (setup) only.

Goal: a clean, runnable skeleton that every later phase builds on.

Do the following:
1. Create the repository structure from PROJECT_SPEC.md with empty or minimal modules (each file gets a one-line docstring saying its purpose).
2. Create docker-compose.yml with a PostgreSQL 16 service (named volume, healthcheck, credentials read from a .env file). Add .env.example and add .env to .gitignore.
3. Write db/schema.sql with all six tables from the spec: primary keys, foreign keys, NOT NULL and CHECK constraints where sensible (e.g. check_type IN ('tcp','http'), percentages between 0 and 100). Do not add the performance indexes yet; we add those in Phase 4 so I can measure their effect.
4. Write a small script (scripts/init_db.py or a Makefile target) that applies schema.sql to the running database.
5. Set up pytest with one passing smoke test that connects to the database.
6. requirements.txt with pinned versions, a README stub with setup steps, and an empty CONCEPTS.md containing the table header: Feature | File | Subject | Concept | Interview one-liner.
7. Initialise git and make the first commit.

Before writing schema.sql, explain in a few sentences how the schema satisfies 3NF and why each foreign key exists.

Done when: `docker compose up -d` starts Postgres, the init script creates all tables, and `pytest` passes. Then stop and give me a summary of what you built and the exact commands to run it myself.
