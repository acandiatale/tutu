package com.example.catalog.dao;

import java.util.List;

import com.example.catalog.model.Item;
import org.springframework.orm.ibatis.support.SqlMapClientDaoSupport;

/** iBATIS 2 DAO in the Spring 3 style: the sqlMapClient is injected in applicationContext.xml. */
public class CatalogDao extends SqlMapClientDaoSupport {

    @SuppressWarnings("unchecked")
    public List<Item> findAll() {
        return (List<Item>) getSqlMapClientTemplate().queryForList("catalog.findAll");
    }

    public Item findById(Long id) {
        return (Item) getSqlMapClientTemplate().queryForObject("catalog.findById", id);
    }
}
