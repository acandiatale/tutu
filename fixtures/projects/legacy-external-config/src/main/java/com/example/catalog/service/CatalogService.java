package com.example.catalog.service;

import java.io.File;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import com.example.catalog.repository.CatalogDao;

@Service
public class CatalogService {

    private final CatalogDao dao;

    @Value("${catalog.title}")
    private String title;

    @Value("${mail.host}")
    private String mailHost;

    @Value("${catalog.page-size:10}")
    private int pageSize;

    @Autowired
    public CatalogService(CatalogDao dao) {
        this.dao = dao;
    }

    /** Export directory: set with -Dcatalog.home in Tomcat's setenv.sh, else the working directory. */
    public static File exportDir() {
        String home = System.getProperty("catalog.home");
        if (home == null || home.isEmpty()) {
            home = ".";
        }
        return new File(home, "export");
    }

    public static int clampPageSize(int requested, int max) {
        if (requested < 1) {
            return 1;
        }
        return Math.min(requested, max);
    }

    public String getTitle() {
        return title;
    }

    public String getMailHost() {
        return mailHost;
    }

    public int getPageSize() {
        return pageSize;
    }

    public CatalogDao getDao() {
        return dao;
    }
}
