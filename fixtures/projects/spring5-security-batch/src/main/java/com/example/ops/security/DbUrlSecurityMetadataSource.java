package com.example.ops.security;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import javax.servlet.http.HttpServletRequest;

import org.springframework.security.access.ConfigAttribute;
import org.springframework.security.access.SecurityConfig;
import org.springframework.security.web.FilterInvocation;
import org.springframework.security.web.access.intercept.FilterInvocationSecurityMetadataSource;
import org.springframework.security.web.util.matcher.AntPathRequestMatcher;
import org.springframework.security.web.util.matcher.RequestMatcher;

/**
 * URL → role table (the rows would come from an AUTH_URL table). AntPathRequestMatcher is gone in Spring Security 7:
 * the handler security-path-pattern-matcher rewrites it to PathPatternRequestMatcher.
 */
public class DbUrlSecurityMetadataSource implements FilterInvocationSecurityMetadataSource {

    private final Map<RequestMatcher, Collection<ConfigAttribute>> rules = new LinkedHashMap<>();

    public DbUrlSecurityMetadataSource() {
        load("/admin/**", "GET", "ROLE_ADMIN");
        load("/report/**", null, "ROLE_USER");
    }

    void load(String url, String method, String role) {
        rules.put(new AntPathRequestMatcher(url, method), SecurityConfig.createList(role));
    }

    @Override
    public Collection<ConfigAttribute> getAttributes(Object object) {
        HttpServletRequest request = ((FilterInvocation) object).getRequest();
        for (Map.Entry<RequestMatcher, Collection<ConfigAttribute>> rule : rules.entrySet()) {
            if (rule.getKey().matches(request)) {
                return rule.getValue();
            }
        }
        return null;
    }

    @Override
    public Collection<ConfigAttribute> getAllConfigAttributes() {
        List<ConfigAttribute> all = new ArrayList<>();
        for (Collection<ConfigAttribute> attrs : rules.values()) {
            all.addAll(attrs);
        }
        return all;
    }

    @Override
    public boolean supports(Class<?> clazz) {
        return FilterInvocation.class.isAssignableFrom(clazz);
    }
}
