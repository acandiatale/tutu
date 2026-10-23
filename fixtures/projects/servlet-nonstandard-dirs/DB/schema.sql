-- run by the DBA, never packaged
CREATE TABLE report_row (
    id      BIGINT PRIMARY KEY,
    month   CHAR(7) NOT NULL,
    total   DECIMAL(15, 2) NOT NULL
);
