-- Выполняется один раз при создании тома БД.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE DATABASE fokus_test OWNER fokus;
\connect fokus_test
CREATE EXTENSION IF NOT EXISTS vector;
