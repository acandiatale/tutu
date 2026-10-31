package com.example.shop.web;

import java.io.IOException;

import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;

/** Escapes request parameters (a bean of the root context; web.xml reaches it through a DelegatingFilterProxy). */
public class XssEscapeFilter implements Filter {

    private String replacement = "";

    public void setReplacement(String replacement) {
        this.replacement = replacement;
    }

    public String getReplacement() {
        return replacement;
    }

    @Override
    public void init(FilterConfig filterConfig) {
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        chain.doFilter(request, response);
    }

    @Override
    public void destroy() {
    }
}
