---
title: Spring Security 5 -> 6 -> 7
keywords:
  - "WebSecurityConfigurerAdapter|SecurityFilterChain|HttpSecurity|WebSecurityCustomizer"
  - "authorizeRequests|authorizeHttpRequests|antMatchers|mvcMatchers|regexMatchers|requestMatchers"
  - "\\.and\\(\\)|csrf\\(\\)|cors\\(\\)|formLogin\\(\\)|httpBasic\\(\\)|sessionManagement\\(\\)|oauth2Login\\(\\)|oauth2ResourceServer\\(\\)|exceptionHandling\\(\\)|headers\\(\\)|logout\\(\\)"
  - "EnableGlobalMethodSecurity|EnableMethodSecurity|@Secured|@PreAuthorize"
  - "AuthenticationManagerBuilder|AuthenticationManager|AuthenticationConfiguration|DaoAuthenticationProvider"
  - "PasswordEncoder|NoOpPasswordEncoder|withDefaultPasswordEncoder|BCryptPasswordEncoder"
  - "AntPathRequestMatcher|MvcRequestMatcher|PathPatternRequestMatcher"
  - "AccessDecisionManager|AccessDecisionVoter|FilterSecurityInterceptor|FilterInvocationSecurityMetadataSource|SecurityMetadataSource|ConfigAttribute|spring-security-access"
  - "org\\.springframework\\.security"
  - "EnableWebSecurity"
  - "access\\(\"|WebExpressionAuthorizationManager"
cases: [spring_boot, spring_legacy, msa]
phases: [spring]
---
# Spring Security 5.x → 6.x → 7.x (Boot 4.1 = Security 7.1)

## Configuration model
- `WebSecurityConfigurerAdapter` is gone → plain `@Configuration @EnableWebSecurity` class with beans:
```java
@Bean
SecurityFilterChain securityFilterChain(HttpSecurity http) throws Exception {
    http
        .csrf(csrf -> csrf.disable())
        .authorizeHttpRequests(auth -> auth
            .requestMatchers("/public/**", "/actuator/health").permitAll()
            .requestMatchers(HttpMethod.POST, "/api/**").hasRole("ADMIN")
            .anyRequest().authenticated())
        .httpBasic(Customizer.withDefaults())
        .formLogin(form -> form.loginPage("/login").permitAll());
    return http.build();
}
```
- `configure(WebSecurity web)` → `@Bean WebSecurityCustomizer webSecurityCustomizer() { return web -> web.ignoring().requestMatchers("/static/**"); }`
- `configure(AuthenticationManagerBuilder)` with in-memory/JDBC users → `@Bean UserDetailsService` (`InMemoryUserDetailsManager`,
  `JdbcUserDetailsManager`) + `@Bean PasswordEncoder`. Need an `AuthenticationManager` bean →
  `@Bean AuthenticationManager authenticationManager(AuthenticationConfiguration c) throws Exception { return c.getAuthenticationManager(); }`
- `authenticationManagerBean()` override → the bean above. Custom `AuthenticationProvider` → expose as `@Bean`.
- `@EnableWebSecurity` no longer meta-annotated with `@Configuration` (6.0) → add `@Configuration`.
- Multiple chains: one `SecurityFilterChain` per `http.securityMatcher("/api/**")`, ordered with `@Order` (lowest first, catch-all last).

## Lambda DSL only (Security 7 removed the chained style)
- `.and()` removed; every `http.xxx()` no-arg configurer removed → `http.xxx(c -> …)` or `Customizer.withDefaults()`.
- `csrf().disable()` → `csrf(csrf -> csrf.disable())` (or `csrf(AbstractHttpConfigurer::disable)`);
  `cors()` → `cors(Customizer.withDefaults())`; `headers().frameOptions().sameOrigin()` → `headers(h -> h.frameOptions(f -> f.sameOrigin()))`;
  `sessionManagement().sessionCreationPolicy(STATELESS)` → `sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))`;
  `exceptionHandling().authenticationEntryPoint(ep)` → `exceptionHandling(e -> e.authenticationEntryPoint(ep))`;
  `oauth2ResourceServer().jwt()` → `oauth2ResourceServer(o -> o.jwt(Customizer.withDefaults()))`; `logout().logoutUrl(u)` → `logout(l -> l.logoutUrl(u))`.
- `authorizeRequests()` → `authorizeHttpRequests()`; `antMatchers`/`mvcMatchers`/`regexMatchers` → `requestMatchers`
  (`regexMatchers` → `requestMatchers(RegexRequestMatcher.regexMatcher("…"))`).
- `.access("hasRole('A') and hasIpAddress('…')")` String → `.access(new WebExpressionAuthorizationManager("…"))`.
- `AntPathRequestMatcher` / `MvcRequestMatcher` removed in 7 → `PathPatternRequestMatcher.pathPattern("/x/**")` /
  `pathPattern(HttpMethod.POST, "/x/**")` (= `withDefaults().matcher(…)`;
  `org.springframework.security.web.servlet.util.matcher.PathPatternRequestMatcher`) or plain String patterns: covers
  `new AntPathRequestMatcher(p[, m])`, `AntPathRequestMatcher.antMatcher(…)` and `new MvcRequestMatcher(introspector, p)`.
  A literal pattern with `**` before the last segment (`/**/*.do`, `/api/**/list`) needs a `// MIGRATION-TODO:` line —
  PathPattern rejects it at startup (`/{*path}` or explicit segments). The 3-/4-argument (case-insensitive,
  UrlPathHelper) constructors need a hand-written matcher.
- Legacy access-decision API (eGovFrame-style DB URL authorization: `FilterSecurityInterceptor`,
  `FilterInvocationSecurityMetadataSource`, `AccessDecisionManager` / `AffirmativeBased` / `RoleVoter`,
  `WebExpressionVoter`, `SecurityMetadataSource`, `ConfigAttribute`) moved to `org.springframework.security:spring-security-access`
  in 7.x (same packages; spring-security-config / -web do not depend on it). `add-known-dependency` adds it
  (Boot-managed through spring-security-bom) when javac reports one of those classes/packages; the long-term target is
  `AuthorizationFilter` + `AuthorizationManager<RequestAuthorizationContext>`.

## Method security
- `@EnableGlobalMethodSecurity(prePostEnabled = true, securedEnabled = true)` → `@EnableMethodSecurity(securedEnabled = true)`
  (prePost enabled by default; `jsr250Enabled` same name).

## Semantics to keep in mind (add MIGRATION-TODO if a change touches them)
- `hasRole("ADMIN")` checks authority `ROLE_ADMIN`; passing `"ROLE_ADMIN"` to `hasRole` throws → use `hasAuthority("ROLE_ADMIN")` or strip prefix.
- `authorizeHttpRequests` applies to all dispatch types (FORWARD/ERROR) — error pages may need `.dispatcherTypeMatchers(ERROR).permitAll()`.
- `SecurityContextHolderFilter` requires explicit save: `SecurityContextRepository` when authenticating manually.
- `DaoAuthenticationProvider`: use constructor `new DaoAuthenticationProvider(userDetailsService)`; `setUserDetailsService` deprecated/removed.
- `User.withDefaultPasswordEncoder()` is deprecated, test-only; `NoOpPasswordEncoder` deprecated — keep only if as-is used it, MIGRATION-TODO.
- Password storage: `PasswordEncoderFactories.createDelegatingPasswordEncoder()` expects `{bcrypt}` prefixes.

## OAuth2 starters (Boot 4)
`spring-boot-starter-oauth2-client` → `spring-boot-starter-security-oauth2-client`; `-oauth2-resource-server` →
`-security-oauth2-resource-server`; `-oauth2-authorization-server` → `-security-oauth2-authorization-server`.
