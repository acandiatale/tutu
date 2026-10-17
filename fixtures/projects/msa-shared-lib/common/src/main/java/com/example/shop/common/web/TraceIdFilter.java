package com.example.shop.common.web;

import java.io.IOException;

import javax.annotation.PostConstruct;
import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import com.example.shop.common.TraceIds;

/** Propagates (or creates) the X-Trace-Id header on every request. */
public class TraceIdFilter implements Filter {

    private String prefix;

    @PostConstruct
    public void init() {
        prefix = System.getProperty("shop.trace.prefix", "trc");
    }

    @Override
    public void init(FilterConfig filterConfig) {
        if (prefix == null) {
            init();
        }
    }

    @Override
    public void doFilter(ServletRequest req, ServletResponse res, FilterChain chain)
            throws IOException, ServletException {
        HttpServletRequest request = (HttpServletRequest) req;
        String traceId = request.getHeader(TraceIds.HEADER);
        if (traceId == null || traceId.isEmpty()) {
            traceId = TraceIds.newId(prefix == null ? "trc" : prefix);
        }
        request.setAttribute(TraceIds.HEADER, traceId);
        ((HttpServletResponse) res).setHeader(TraceIds.HEADER, traceId);
        chain.doFilter(req, res);
    }

    @Override
    public void destroy() {
        // nothing to release
    }
}
