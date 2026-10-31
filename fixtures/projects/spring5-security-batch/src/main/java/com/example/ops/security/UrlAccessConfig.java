package com.example.ops.security;

import java.util.ArrayList;
import java.util.List;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.access.AccessDecisionManager;
import org.springframework.security.access.AccessDecisionVoter;
import org.springframework.security.access.vote.AffirmativeBased;
import org.springframework.security.access.vote.AuthenticatedVoter;
import org.springframework.security.access.vote.RoleVoter;
import org.springframework.security.web.access.intercept.FilterInvocationSecurityMetadataSource;

/**
 * eGovFrame-style URL authorization read from the database: the legacy access-decision API that Spring Security 7
 * moved into spring-security-access (the fix loop adds that dependency, version managed).
 */
@Configuration
public class UrlAccessConfig {

    @Bean
    public AccessDecisionManager urlAccessDecisionManager() {
        List<AccessDecisionVoter<?>> voters = new ArrayList<>();
        voters.add(new RoleVoter());
        voters.add(new AuthenticatedVoter());
        return new AffirmativeBased(voters);
    }

    @Bean
    public FilterInvocationSecurityMetadataSource urlSecurityMetadataSource() {
        return new DbUrlSecurityMetadataSource();
    }
}
