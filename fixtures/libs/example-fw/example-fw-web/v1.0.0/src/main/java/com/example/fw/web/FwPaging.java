package com.example.fw.web;

import com.example.fw.core.FwStrings;

/** Paging parameters parsed from plain request strings (no servlet types in the API). */
public final class FwPaging {

    private final int page;
    private final int size;

    private FwPaging(int page, int size) {
        this.page = page;
        this.size = size;
    }

    public static FwPaging of(String page, String size) {
        return new FwPaging(toInt(page, 1), Math.min(toInt(size, 20), 100));
    }

    private static int toInt(String s, int fallback) {
        if (FwStrings.isBlank(s)) {
            return fallback;
        }
        try {
            int v = Integer.parseInt(s.trim());
            return v > 0 ? v : fallback;
        } catch (NumberFormatException e) {
            return fallback;
        }
    }

    public int getPage() {
        return page;
    }

    public int getSize() {
        return size;
    }

    public int getOffset() {
        return (page - 1) * size;
    }
}
