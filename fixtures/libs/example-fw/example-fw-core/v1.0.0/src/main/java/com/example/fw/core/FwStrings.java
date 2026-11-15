package com.example.fw.core;

/** String helpers of the stand-in framework core (no javax / jakarta types). */
public final class FwStrings {

    private FwStrings() {
    }

    public static boolean isBlank(String s) {
        return s == null || s.trim().isEmpty();
    }

    public static String defaultIfBlank(String s, String fallback) {
        return isBlank(s) ? fallback : s.trim();
    }

    public static String mask(String s, int keep) {
        if (s == null || s.length() <= keep) {
            return s;
        }
        StringBuilder sb = new StringBuilder(s.substring(0, keep));
        for (int i = keep; i < s.length(); i++) {
            sb.append('*');
        }
        return sb.toString();
    }
}
