package com.example.ops.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configuration.WebSecurityConfigurerAdapter;

/**
 * Spring Security 5.7 style: removed in Security 6 (WebSecurityConfigurerAdapter, authorizeRequests, antMatchers,
 * and()). The plan's UpgradeSpringSecurity_6_0 (→ 5_8 → 5_7) … 7_0 chain turns it into a SecurityFilterChain bean.
 */
@Configuration
@EnableWebSecurity
public class WebSecurityConfig extends WebSecurityConfigurerAdapter {

    @Override
    protected void configure(HttpSecurity http) throws Exception {
        http.authorizeRequests()
                .antMatchers("/admin/**").hasRole("ADMIN")
                .antMatchers("/batch/**").hasAnyRole("ADMIN", "OPERATOR")
                .anyRequest().authenticated()
                .and()
            .formLogin()
                .loginPage("/login").permitAll()
                .and()
            .csrf().disable();
    }
}
