---
title: Spring Mobile (org.springframework.mobile) is EOL with no Jakarta build — replace it with a small in-house device resolver
keywords:
  - "org\\.springframework\\.mobile"
  - "DeviceUtils|getCurrentDevice|getRequiredCurrentDevice"
  - "DeviceResolverHandlerInterceptor|DeviceResolverRequestFilter|LiteDeviceResolver"
  - "SitePreference|DeviceWebArgumentResolver|DevicePlatform"
cases: [spring_legacy, servlet_tomcat, spring_boot, msa]
phases: [spring]
always: false
replacement: true
---
# Spring Mobile (`org.springframework.mobile`) — replace, do not carry

**Status.** End of life: last release `spring-mobile-device:1.1.5.RELEASE` (2016), the repository is archived in
`spring-attic`. There is **no Spring 5+/6/7 release and no Jakarta build**, and there never will be. Do not look for one, do not
transform the jar's bytecode (javax → jakarta renaming would not help: `DeviceResolverHandlerInterceptor extends
HandlerInterceptorAdapter`, a class Spring Framework 6 removed — the jar fails at runtime on Spring 7 even when it compiles).
Do not keep the old jar in `WEB-INF/lib` / as a `system` dependency.

**What projects actually use** (nearly always only this):

| as-is | meaning |
|---|---|
| `DeviceUtils.getCurrentDevice(request)` / `DeviceUtils.getRequiredCurrentDevice(request)` | the `Device` resolved for this request |
| `Device.isMobile()` / `isTablet()` / `isNormal()` / `getDevicePlatform()` | the three device classes (+ platform: IOS / ANDROID / UNKNOWN) |
| `DeviceResolverHandlerInterceptor` (`<mvc:interceptors>` bean or `WebMvcConfigurer`) / `DeviceResolverRequestFilter` (web.xml) | resolves the device once per request into a request attribute |
| `LiteDeviceResolver` | the User-Agent heuristics behind it |
| `SitePreferenceHandlerInterceptor`, `SitePreferenceUtils`, `SitePreference` | the user's "show me the desktop site" cookie / parameter (rare) |
| `DeviceWebArgumentResolver` / `@Device` controller parameters | `Device` as a handler-method argument (rare) |

**The fix — answer `<no_script>` + `<edits>`, NOT a `<script>`.** The replacement is project code (new classes under the
project's package, rewritten imports): no other project can reuse a script for it, and a script that creates classes
and rewrites imports fails more often than direct edits do. Give the new files as `op: write` edits, the import changes
as `replace` edits (one per file), the pom change as a `pom` edit; the attempt is recorded in the report and the case
history as edits. Replace the library with a small in-house `compat` package that keeps the same type and method
names, so the call sites only change their `import` lines:

1. Add under the project's base package (e.g. `com.example.app.compat.device`, in the module that compiles the controllers):
   * `Device` — an interface with `isNormal()`, `isMobile()`, `isTablet()`, `getDevicePlatform()`; a `DevicePlatform` enum
     (`IOS`, `ANDROID`, `UNKNOWN`); a `LiteDevice` value class (static factories `mobile(platform)`, `tablet(platform)`, `normal()`).
   * `LiteDeviceResolver` — `Device resolveDevice(HttpServletRequest request)` from the `User-Agent` header (keep it close to
     Spring Mobile's heuristics: tablet first — `ipad`, `android` without `mobile`, `kindle`, `silk`, `playbook`, `tablet`;
     then mobile — the header starts with one of the known mobile prefixes or contains `mobile`, `iphone`, `ipod`,
     `android`, `blackberry`, `windows phone`, `opera mini`, `iemobile`; platform from `iphone|ipad|ipod` → IOS,
     `android` → ANDROID). Imports are `jakarta.servlet.http.HttpServletRequest`.
   * `DeviceUtils` — `getCurrentDevice(HttpServletRequest)` reads the request attribute
     `DeviceUtils.CURRENT_DEVICE_ATTRIBUTE` (= `"currentDevice"`) and falls back to resolving on the spot;
     `getRequiredCurrentDevice` throws `IllegalStateException` when absent — same contract as before.
   * `DeviceResolverHandlerInterceptor implements org.springframework.web.servlet.HandlerInterceptor` (NOT
     `HandlerInterceptorAdapter`) — `preHandle` stores the resolved device under the attribute and returns `true`.
     If the project used `DeviceResolverRequestFilter`, write the same as a `jakarta.servlet.Filter` (or
     `OncePerRequestFilter`) instead.
   * Only when the project used them: `SitePreference` / `SitePreferenceUtils` / `SitePreferenceHandlerInterceptor`
     (a cookie `org.springframework.mobile.device.site.CookieSitePreferenceRepository.SITE_PREFERENCE` or a
     `site_preference` request parameter → `NORMAL` / `MOBILE` / `TABLET`), and a `HandlerMethodArgumentResolver` for
     `Device` parameters registered in `WebMvcConfigurer.addArgumentResolvers`.
2. Rewrite every `import org.springframework.mobile.device.<X>;` (and `…device.site.<X>`, `…device.util.<X>`) to the compat
   package; qualified names in XML bean definitions (`<bean class="org.springframework.mobile.device.DeviceResolverHandlerInterceptor"/>`
   inside `<mvc:interceptors>`) and in `web.xml` filter classes get the compat class name. Call sites stay as they are.
3. Remove the `org.springframework.mobile:*` dependency (and a `spring-mobile-device-*.jar` wired from `WEB-INF/lib`) from the
   poms. Nothing else in the project references the library.
4. Report in `<analysis>` which of the rare parts (SitePreference, argument resolver, filter) the project used, and in
   `<solution>` that device detection is now a local User-Agent heuristic the owning team maintains (this reaches the
   report and the case history).

**Do not**: keep `spring-mobile-device` at any version, add a `-jakarta` guess, edit the jar, or declare the
device logic "out of scope" — the replacement above is small (≈150 lines) and the owners asked for a building project.

Three more rules, from a company run (2026-11-06) that ended blocked:

* **Never answer `<blocked>` for this library.** The fix is project code; sbmig rejects such a decline and asks again.
* **Put the compat classes under the project's package, never under `org.springframework.mobile.*`.** Shadowing the library's
  own package leaves every class you did not write coming from the old jar with its javax signatures (those are the errors that
  remain), keeps the jar in the build, and hides the replacement from the owners.
* **One answer does the whole job**: the compat files, *every* import rewrite (and the class names in XML / web.xml), and the
  pom edit. An answer that leaves the errors as they are is scored `no_change` and rolled back — the next ask starts from the
  as-is tree, so "files first, call sites next" never lands.
