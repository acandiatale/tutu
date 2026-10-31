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
  - "authentication\\.encoding|ShaPasswordEncoder|Md5PasswordEncoder|SaltSource|EnableWebMvcSecurity"
  - "org\\.springframework\\.security\\.oauth2\\.(config|provider)|EnableResourceServer|EnableAuthorizationServer|EnableOAuth2Sso|OAuth2RestTemplate|ResourceServerConfigurerAdapter"
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
  PathPattern (7.0.x) accepts `**` at the start or the end (`/**/*.do`, `/api/**` — keep them); a `**` in the middle
  (`/api/**/list`, `/admin/**/*.do`) is rejected at startup → `/{*path}` or explicit segments + `// MIGRATION-TODO:`.
  The 3-/4-argument (case-insensitive, UrlPathHelper) constructors need a hand-written matcher.
- Legacy access-decision API (eGovFrame-style DB URL authorization: `FilterSecurityInterceptor`,
  `FilterInvocationSecurityMetadataSource`, `AccessDecisionManager` / `AffirmativeBased` / `RoleVoter`,
  `WebExpressionVoter`, `SecurityMetadataSource`, `ConfigAttribute`) moved to `org.springframework.security:spring-security-access`
  in 7.x (same packages; spring-security-config / -web do not depend on it): when javac reports one of those classes,
  declare `org.springframework.security:spring-security-access` with `version: null` (the Security BOM manages it);
  the long-term target (owners) is `AuthorizationFilter` + `AuthorizationManager<RequestAuthorizationContext>`.

## Method security
- `@EnableGlobalMethodSecurity` still compiles in 7.1 (deprecated; the 5.8 recipe converts it) — not a build error by
  itself. When touched: `@EnableGlobalMethodSecurity(prePostEnabled = true, securedEnabled = true)` →
  `@EnableMethodSecurity(securedEnabled = true)` (prePost enabled by default; `jsr250Enabled` same name).

## Security 3 / 4 era types (the recipe chain starts at 5.7)
- `org.springframework.security.authentication.encoding.*` (`ShaPasswordEncoder`, `Md5PasswordEncoder`,
  `PlaintextPasswordEncoder`, `SaltSource`; gone since 5.0) has no hash-compatible drop-in: implement
  `org.springframework.security.crypto.password.PasswordEncoder` so the stored hashes keep matching (same algorithm,
  the `password{salt}` merge when a `SaltSource` was used, hex or Base64 as before), add
  `// MIGRATION-TODO: password hash compatibility` and a `<verify>` line (log in with an existing account).
- `@EnableWebMvcSecurity` → `@EnableWebSecurity` (+ `@Configuration`).
- `SecurityJackson2Modules` (Jackson 2, deprecated) → `org.springframework.security.jackson.SecurityJacksonModules` (Jackson 3).

## Semantics to keep in mind (add MIGRATION-TODO if a change touches them)
- `hasRole("ADMIN")` checks authority `ROLE_ADMIN`; passing `"ROLE_ADMIN"` to `hasRole` throws → use `hasAuthority("ROLE_ADMIN")` or strip prefix.
- `authorizeHttpRequests` applies to all dispatch types (FORWARD/ERROR) — error pages may need `.dispatcherTypeMatchers(ERROR).permitAll()`.
- `SecurityContextHolderFilter` requires explicit save: `SecurityContextRepository` when authenticating manually.
- `DaoAuthenticationProvider`: the no-arg constructor and `setUserDetailsService` were REMOVED in 7.0 →
  `new DaoAuthenticationProvider(userDetailsService)` + `setPasswordEncoder(encoder)`.
- `User.withDefaultPasswordEncoder()` is deprecated, test-only; `NoOpPasswordEncoder` deprecated — keep only if as-is used it, MIGRATION-TODO.
- Password storage: `PasswordEncoderFactories.createDelegatingPasswordEncoder()` expects `{bcrypt}` prefixes.

## OAuth2 starters (Boot 4)
`spring-boot-starter-oauth2-client` → `spring-boot-starter-security-oauth2-client`; `-oauth2-resource-server` →
`-security-oauth2-resource-server`; `-oauth2-authorization-server` → `-security-oauth2-authorization-server`.

## fix-loop: spring-security-oauth2-legacy
`org.springframework.security.oauth2.config.annotation.*` / `.provider.*` / `OAuth2RestTemplate` (the discontinued
spring-security-oauth2 2.x) do not exist on Security 7; never re-add that jar.
- Resource server (`@EnableResourceServer`, `ResourceServerConfigurerAdapter`) → a `SecurityFilterChain` bean with
  `http.oauth2ResourceServer(o -> o.jwt(Customizer.withDefaults()))` (or `opaqueToken(...)`) and the same URL rules;
  starter `spring-boot-starter-security-oauth2-resource-server` (`version: null`); token settings →
  `spring.security.oauth2.resourceserver.*` + MIGRATION-TODO.
- SSO client (`@EnableOAuth2Sso`) → `http.oauth2Login(Customizer.withDefaults())` + `spring-boot-starter-security-oauth2-client`.
- `OAuth2RestTemplate` → `RestClient` with `OAuth2ClientHttpRequestInterceptor` (Security 6.4+), or keep the callers
  compiling behind a MIGRATION-TODO.
- Authorization server (`@EnableAuthorizationServer`, `AuthorizationServerConfigurerAdapter`, token stores): no port
  inside the build fix — answer `<blocked>` (the owners move to Spring Authorization Server).
- Each port above replaces a library: write `<verify>` (token validation, login flow).
