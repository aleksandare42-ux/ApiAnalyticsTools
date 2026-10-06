-- Read-only role for BI tools (Tableau, Metabase, DBeaver...): analyst / analyst
CREATE ROLE analyst LOGIN PASSWORD 'analyst';
GRANT CONNECT ON DATABASE opanalytics TO analyst;
GRANT USAGE ON SCHEMA public TO analyst;
ALTER DEFAULT PRIVILEGES FOR ROLE opa IN SCHEMA public GRANT SELECT ON TABLES TO analyst;
