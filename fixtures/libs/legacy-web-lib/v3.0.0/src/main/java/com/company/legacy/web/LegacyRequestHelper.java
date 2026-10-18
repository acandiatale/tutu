package com.company.legacy.web;

import jakarta.servlet.http.HttpServletRequest;

/**
 * Request helper shared by the company's web applications.
 * Version 3.0.0 exposes {@code jakarta.servlet.http.HttpServletRequest} in its public API and renames
 * {@code clientIp(HttpServletRequest)} / {@code clientIp()} (2.x) to {@code remoteClientIp(HttpServletRequest)} /
 * {@code remoteClientIp()}; there is no deprecated alias.
 */
public class LegacyRequestHelper {

    private static final String FORWARDED_FOR = "X-Forwarded-For";

    private final HttpServletRequest request;

    public LegacyRequestHelper(HttpServletRequest request) {
        this.request = request;
    }

    /** Client IP, honouring the first {@code X-Forwarded-For} hop set by the load balancer. */
    public static String remoteClientIp(HttpServletRequest req) {
        String forwarded = req.getHeader(FORWARDED_FOR);
        if (forwarded != null && !forwarded.trim().isEmpty()) {
            int comma = forwarded.indexOf(',');
            return (comma < 0 ? forwarded : forwarded.substring(0, comma)).trim();
        }
        return req.getRemoteAddr();
    }

    public String remoteClientIp() {
        return remoteClientIp(request);
    }

    public String requestKey() {
        return request.getMethod() + " " + request.getRequestURI();
    }

    public boolean isAjax() {
        return "XMLHttpRequest".equals(request.getHeader("X-Requested-With"));
    }
}
