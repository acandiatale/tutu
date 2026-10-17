package com.example.board.web;

import java.io.IOException;
import java.util.logging.Logger;

import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import javax.servlet.http.HttpServletRequest;

public class LoggingFilter implements Filter {

    private static final Logger LOG = Logger.getLogger(LoggingFilter.class.getName());

    private String prefix = "[board]";

    @Override
    public void init(FilterConfig filterConfig) throws ServletException {
        String p = filterConfig.getInitParameter("prefix");
        if (p != null) {
            prefix = p;
        }
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        long start = System.currentTimeMillis();
        try {
            chain.doFilter(request, response);
        } finally {
            if (request instanceof HttpServletRequest) {
                HttpServletRequest http = (HttpServletRequest) request;
                LOG.info(prefix + " " + http.getMethod() + " " + http.getRequestURI() + " "
                        + (System.currentTimeMillis() - start) + "ms");
            }
        }
    }

    @Override
    public void destroy() {
        // nothing
    }
}
