package com.example.catalog;

import com.example.catalog.dao.CatalogDao;
import com.example.catalog.model.Item;
import org.springframework.context.support.ClassPathXmlApplicationContext;

/** Batch entry point: loads the XML context and prints the catalog. */
public class Main {

    public static void main(String[] args) {
        ClassPathXmlApplicationContext ctx = new ClassPathXmlApplicationContext("applicationContext.xml");
        try {
            CatalogDao dao = ctx.getBean("catalogDao", CatalogDao.class);
            for (Item item : dao.findAll()) {
                System.out.println(item);
            }
        } finally {
            ctx.close();
        }
    }
}
