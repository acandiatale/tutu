package com.example.catalog.repository;

import javax.sql.DataSource;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Repository;

/** Keeps the container DataSource (JNDI java:comp/env/jdbc/catalogDS); queries are out of scope for the fixture. */
@Repository
public class CatalogDao {

    private final DataSource dataSource;

    @Autowired
    public CatalogDao(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    public DataSource getDataSource() {
        return dataSource;
    }
}
