package com.example.members.web;

import java.io.IOException;

import javax.servlet.FilterChain;
import javax.servlet.ServletException;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

import com.company.legacy.web.LegacyRequestHelper;

/** Exposes the caller IP (as seen by the in-house helper) to downstream code and the access log. */
@Component
public class ClientIpFilter extends OncePerRequestFilter {

    public static final String CLIENT_IP_ATTRIBUTE = "member-api.clientIp";

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response, FilterChain chain)
            throws ServletException, IOException {
        request.setAttribute(CLIENT_IP_ATTRIBUTE, LegacyRequestHelper.clientIp(request));
        chain.doFilter(request, response);
    }
}
