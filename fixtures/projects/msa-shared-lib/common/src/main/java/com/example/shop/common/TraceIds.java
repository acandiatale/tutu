package com.example.shop.common;

import java.util.UUID;

/** Servlet-free id helper shared by all services. */
public final class TraceIds {

    public static final String HEADER = "X-Trace-Id";

    private TraceIds() {
    }

    public static String newId(String prefix) {
        return prefix + "-" + UUID.randomUUID().toString().replace("-", "").substring(0, 16);
    }
}
