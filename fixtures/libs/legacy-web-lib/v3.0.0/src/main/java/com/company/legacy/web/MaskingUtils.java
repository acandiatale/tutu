package com.company.legacy.web;

/** Servlet-free helper: masks personal data before it is logged. */
public final class MaskingUtils {

    private MaskingUtils() {
    }

    public static String maskEmail(String email) {
        if (email == null) {
            return null;
        }
        int at = email.indexOf('@');
        if (at <= 1) {
            return email;
        }
        return email.charAt(0) + repeat('*', at - 1) + email.substring(at);
    }

    private static String repeat(char c, int n) {
        StringBuilder sb = new StringBuilder(n);
        for (int i = 0; i < n; i++) {
            sb.append(c);
        }
        return sb.toString();
    }
}
