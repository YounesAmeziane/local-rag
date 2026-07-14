-- create_readonly_login.sql — dedicated read-only login for the RAG SQL path (audit #3).
--
-- Run as an admin on the target SQL Server, then set DB_READONLY_USER and
-- DB_READONLY_PASSWORD in .env. Once set, sql_generator.execute_sql() and the
-- enumerate path connect as this login instead of the service's Windows identity,
-- so a validator bypass cannot write, alter, or execute anything.
--
-- Least privilege: this grants read across the DB via db_datareader for simplicity.
-- Tighten to per-schema GRANT SELECT (excluding PII schemas) once RBAC lands.

USE [master];
GO
IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'rag_readonly')
    CREATE LOGIN [rag_readonly]
        WITH PASSWORD = N'CHANGE_ME_TO_A_STRONG_PASSWORD', CHECK_POLICY = ON;
GO

USE [MetadataRepository];
GO
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'rag_readonly')
    CREATE USER [rag_readonly] FOR LOGIN [rag_readonly];
GO

-- Read-only across the database.
ALTER ROLE [db_datareader] ADD MEMBER [rag_readonly];
GO

-- Belt-and-suspenders: explicitly deny writes / DDL / proc execution.
DENY INSERT, UPDATE, DELETE, EXECUTE, ALTER TO [rag_readonly];
GO

-- Optional least-privilege alternative (comment out db_datareader above and use this):
--   GRANT SELECT ON SCHEMA::[dq]    TO [rag_readonly];
--   GRANT SELECT ON SCHEMA::[dm_dq] TO [rag_readonly];
--   GRANT SELECT ON SCHEMA::[rpt]   TO [rag_readonly];
--   -- ... but NOT schemas holding PII/HR data, once those are identified.
