package com.acme.common;

public final class Util {
    private Util() {
    }

    public static boolean isEmpty(String s) {
        return s == null || s.trim().length() == 0;
    }
}
