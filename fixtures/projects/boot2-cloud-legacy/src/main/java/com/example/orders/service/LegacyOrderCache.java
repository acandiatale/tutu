package com.example.orders.service;

import org.springframework.stereotype.Component;

import net.sf.ehcache.Cache;
import net.sf.ehcache.CacheManager;
import net.sf.ehcache.Element;

/** Ehcache 2 (net.sf.ehcache) — version managed by the Boot 2.7 parent, not by Boot 3+/4. */
@Component
public class LegacyOrderCache {

    private final CacheManager manager = CacheManager.create();

    public LegacyOrderCache() {
        if (manager.getCache("orders") == null) {
            manager.addCache("orders");
        }
    }

    public void put(String id, String payload) {
        cache().put(new Element(id, payload));
    }

    public String get(String id) {
        Element e = cache().get(id);
        return e == null ? null : String.valueOf(e.getObjectValue());
    }

    private Cache cache() {
        return manager.getCache("orders");
    }
}
