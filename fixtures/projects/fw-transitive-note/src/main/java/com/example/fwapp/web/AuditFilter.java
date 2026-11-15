package com.example.fwapp.web;

import java.io.IOException;

import javax.servlet.Filter;
import javax.servlet.FilterChain;
import javax.servlet.FilterConfig;
import javax.servlet.ServletException;
import javax.servlet.ServletRequest;
import javax.servlet.ServletResponse;
import javax.servlet.http.HttpServletRequest;

import com.example.fw.core.FwStrings;

public class AuditFilter implements Filter {

    @Override
    public void init(FilterConfig filterConfig) {
        // nothing to configure
    }

    @Override
    public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
            throws IOException, ServletException {
        HttpServletRequest http = (HttpServletRequest) request;
        String user = FwStrings.mask(FwStrings.defaultIfBlank(http.getRemoteUser(), "anonymous"), 3);
        request.setAttribute("auditUser", user);
        chain.doFilter(request, response);
    }

    @Override
    public void destroy() {
        // nothing to release
    }
}
