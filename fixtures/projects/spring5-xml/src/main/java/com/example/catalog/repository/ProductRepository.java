package com.example.catalog.repository;

import java.util.List;

import javax.sql.DataSource;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.stereotype.Repository;

import com.example.catalog.model.Product;

@Repository
public class ProductRepository {

    private static final RowMapper<Product> MAPPER = (rs, i) ->
            new Product(rs.getLong("id"), rs.getString("name"), rs.getBigDecimal("price"));

    private final JdbcTemplate jdbcTemplate;

    @Autowired
    public ProductRepository(DataSource dataSource) {
        this.jdbcTemplate = new JdbcTemplate(dataSource);
    }

    public List<Product> findAll() {
        return jdbcTemplate.query("select id, name, price from product order by id", MAPPER);
    }

    public Product findById(long id) {
        List<Product> list = jdbcTemplate.query("select id, name, price from product where id = ?", MAPPER, id);
        return list.isEmpty() ? null : list.get(0);
    }
}
