# Framework conversion — rules and module contract (`sbmig/fwconvert/`)

Turns a project written on the in-house framework into a generated Spring Boot project: a parent pom and four modules
(boot / domain / service / store). The framework is gone from the result. This is the second of the two ways a
framework project is handled; the first (the framework stays, JDK 21 and jakarta only) is `agent-doc/13`.

**State of the code.** `sbmig/fwconvert/` holds the converters and `pipeline.convert_project`, which runs them in
order and writes the generated project. A run uses it in mode `convert` (`docs/INTERFACES.md` "v2.1z-cm"): intake
replaces the uploaded tree by the generated project, and the run goes on as an ordinary migration of a multi-module
Spring Boot project. What is still missing is in section 9.

**Where the rules come from.** The company's guide to an earlier in-house tool that does this conversion (Java 17,
Spring Boot 3.5.13). The guide is the specification; that tool's source is not available here. Rules marked
*sbmig* are choices made here where the guide is silent or records a defect of the earlier tool.

## 1. Names that never appear in this repository

The framework's name is a company name. Every rule that needs it uses a token:

| token | value | example with the stand-in name |
|---|---|---|
| `{Fw}` | `framework.name`, else the name made from the artifact (`FrameworkConfig.display_name`) | `Example` |
| `{fw}` | the same, lower case | `example` |

So `@{Fw}Service` is the framework's service annotation and `*.{fw}_sql` its SQL file. The vocabulary lives in
`knowledge/fwconvert/rules.yml`; `sbmig.fwconvert.rules.load_rules(name, overlay)` expands the tokens. Company values
that are not derived from the name come in through the overlay (`ConvertConfig.overlay()`: the `convert` section of
the config). **A company has to set none of them**: the package and the project name come from the uploaded project's
root pom, and a project's own activity classes are handled from evidence inside the project (2.2). The overlay keys
stay as refinements. Tests and fixtures use `Example`, `com.example`.

## 2. Input

### 2.1 File kinds

| kind | recognised by | what happens |
|---|---|---|
| `java` | `*.java` | classified (2.4); DAO / service / controller code is regenerated from the service XML and the mappers, not converted |
| `jsp` | `*.jsp` | copied unchanged |
| `static` | `.css .js .png .jpg .jpeg .gif .ico .svg .bmp .webp .woff .woff2 .ttf .otf .eot .map` | copied; a non-JSP file inside `WEB-INF` is refused and listed |
| `sql` | `*.{fw}_sql` (root element `queryMap`) | converted into a MyBatis mapper (4.1) |
| `mapper` | path contains `mybatis`, extension `.xml` or `.mapper`, text has `<mapper namespace=` | copied, file name ends `.xml` |
| `service_xml` | `*-service.xml` | decomposed (2.2) |
| `db_config` | `applicationContext.xml`, `*-context.xml`, an xml whose name contains `datasource`, `*.properties` | read by the datasource step (2.6) |
| `ignored` | `web.xml` | not needed by Spring Boot |

### 2.2 Service XML

The activity dialect is the one converted:

```xml
<service name="order-service">
  <activity name="getList" class="...{Fw}MyBatisSearch">
    <property name="sql-key" value="order.select"/>
    <property name="result-key" value="rows"/>
    <transition name="..." value="..."/>
  </activity>
  <activity name="router" class="...{Fw}DefaultRouter">
    <transition name="find" value="getList"/>   <!-- transition name = URL part, value = activity name -->
  </activity>
</service>
```

The bean dialect (`<service id=… class=…>` with `<property ref=…>`, `<transaction>`, `<security>`) is not converted.
*sbmig*: such a file is reported as a hand-over item `[BEAN_DIALECT]`, never skipped in silence.

Activity kind, by class-name suffix, case ignored:

| suffix | kind | returns | meaning |
|---|---|---|---|
| `{Fw}MyBatisSearch`, `{Fw}JdbcSearch` | `search` | list | select |
| `{Fw}MyBatisUpdate`, `{Fw}JdbcUpdate` | `update` | int | mutate |
| classes a company lists (overlay `activities.kinds`; none by default) | `upsert` / `delete` / `sortsearch` / `pagesearch` | int / int / list / list | mutate / mutate / select / select |
| class name contains `router` | `router` | no method | defines the transitions |
| company classes (overlay `activities.user_suffixes`) | `user` | no method | session handling |
| anything else | `other` | int when the `sql-key` contains `insert` / `update` / `delete` / `upsert` / `save`, else list | user code |

"list" is `List<Map<String,Object>>`.

*sbmig*, the user's point: every project has its own activity classes, so a table of class names cannot cover them
(the four non-framework classes of the guide's table were its sample project's own). A class that is not in the
table is judged from the project: (1) what it extends: its `extends` chain is followed through the project's
sources, at most five levels, and the first ancestor the table knows gives the kind; (2) its return type and call
come from the SQL statement its `sql-key` reaches (select → list, insert / update / delete → int); (3) the source
steps of 2.3. Such a class becomes a plain statement call, and each one gets ONE hand-over item `[CUSTOM_ACTIVITY]`
(its own parameter handling, loops and validation were not carried over). The special bodies of `upsert`, `delete`,
`sortsearch`, `pagesearch` are generated only for a class a company lists.

### 2.3 Whether an activity uses the database (first match wins)

| step | condition | `db_backed` | `confidence` | `pass_hit` |
|---|---|---|---|---|
| 0 | class name contains `router` | no | `not_db` | `router` |
| 0 | a `user` activity | no | `not_db` | `user` |
| 0 | class name contains a word of overlay `activities.external_calls.<tag>` | no | `external_call` | the tag |
| 1a | a built-in DB activity of the framework (class simple name starts with `{Fw}`, kind `search` / `update`) | yes | `certain` | `1a` |
| 1b | a DB activity whose class simple name starts with a prefix of overlay `activities.extra_db_prefixes` | yes | `certain` | `1b` |
| 1c | any other class with a `sql-key` | yes | `strong` | `1c` |
| 2a | the class's Java source calls `dao.X("ns.id", …)` | yes | `strong` | `2a` |
| 2b | the source only shows a DB API | yes, no statement id | `strong` | `2b` |
| — | source found, no trace of a database | no | `not_db` | `java_only` |
| 3 | source not found | no | `ambiguous` | `3` |

- Step 0 compares the lower-cased fully qualified class name by substring, so a name that merely contains the word
  matches (known weakness of the rule, kept).
- 2a: methods `find`, `findOne`, `findList`, `findOneItem`, `select*`, `update`, `insert`, `delete`, `execute`,
  `count`, `batchUpdate`, `exec*` called with a string literal `"ns.id"` as first argument; only an id with a `.`.
- 2b markers: `SqlSession`, `{Fw}Dao`, `{Fw}MyBatis`, `JdbcTemplate`, `NamedParameterJdbcTemplate`,
  `PreparedStatement`, `getDao(`, `executeQuery`, `executeUpdate`, `@Mapper`.
- Two or more different mapper references in one class: `db_backed` no, `ambiguous`, `pass_hit` `3`.
- Hand-over items are written for the external-call tags, `2b` and `3`. *sbmig*: also for `java_only` (the earlier
  tool left a stub and said nothing, so business code vanished without a trace).

### 2.4 Java components

Kind, first match from the top:

| # | kind | regular expressions over the source text |
|---|---|---|
| 1 | `batch` | `{Fw}ChunkProcessor`, `{Fw}BatchContext`, overlay `components.batch_markers`, `package .+\.batch` |
| 2 | `controller` | `{Fw}Controller`, `{Fw}Action`, `package .+\.controller` |
| 3 | `service` | `{Fw}Service`, `{Fw}Transaction`, `package .+\.service`, `class \w+ServiceImpl` |
| 4 | `dao` | `{Fw}Repository`, `{Fw}BaseDao`, `{Fw}MyBatisDao`, `{Fw}QueryExecutor`, `package .+\.dao`, `extends.*Dao`, `class \w+Dao\b` |
| 5 | `domain` | `package .+\.(domain\|model\|vo\|dto)` |
| — | `unknown` | none of the above |

Risk score, summed and capped at 1.0: `{Fw}ChunkProcessor` 0.25, `{Fw}FileWriter` 0.15, `{Fw}BatchContext` 0.15,
`Class.forName` 0.2, `Method.invoke` 0.2, `Thread.` 0.15, `synchronized` 0.1, `.getInstance()` 0.1, plus overlay
`components.risk` (the earlier tool gave its legacy job runner class 0.4). A score of `components.risk_threshold`
(0.7) or more is a hand-over item `risk_score>=0.7`.

Order: `domain → dao → service → controller → batch → unknown`; inside one kind a topological order by imports
(Kahn; what is left of a cycle is appended as it stands).

### 2.5 Front end

Ajax calls the transformer knows (`t` is a JavaScript expression, usually a variable):

| pattern | source | result |
|---|---|---|
| A | `"…commonAjax.json?ServiceName=X-service&"+t+"=1&rest"` | `"/api/X/"+t+"?rest"` |
| B | `…?ServiceName="+sn+"-service&"+t+"=1…` | `"/api/"+sn+"/"+t+"?…"` |
| C | `…commonAjax.json?"+t+"=1…` (the service name comes from a form field) | `"/api/{inferred}/"+t+"?…"` |
| D | `…commonAjax.json?isPage=1&"+t+"=1"` | `"/api/{inferred}/"+t+"?isPage=1"` |
| E, F | A and B with single quotes | as A and B |

- The service of C / D is inferred, in this order: `doc.ServiceName.value="x-service"` in the file, then
  `var serviceName="x"`, then the JS file's own name (not for files named `common*`).
- When the endpoint name is still in the file afterwards, or the inference fails: the file keeps its original text
  and a hand-over item `js_ajax::{file}` `[AJAX_UNMATCHED]` is written.
- A result never ends in a dangling `?`. *sbmig*: when the query's tail is empty and the expression goes on with
  something that is not a string literal (`… + "=1" + qs`), a `"?"` is put in between (`"/api/x/" + t + "?" + qs`);
  the earlier tool appended the variable straight to the path.
- *sbmig*: the same rewrite runs on inline scripts of the copied JSPs, with the same all-or-nothing rule per file.
  A JSP that still called the old endpoint would reach no controller.

Page routes and REST calls (`FrontendApiMap`):

- A `.mvc` URL gives a page route: lower-case kebab (a name without a lower-case letter is only lower-cased);
  `error` is left out. *sbmig*: every occurrence counts (`href`, a form's `action`, `location.href`, `window.open`
  … in JSPs and JS files), not only `href`, and `page_urls` keeps each route's URLs as they are written, because
  the JSPs are copied unchanged and their links must still reach a controller. An absolute URL is kept as a full
  path; a relative one (`x.mvc`, `../AB1C20/x.mvc`) as written, resolved against `mvc_url_prefix` by the generator.
  URLs inside comments (JS, JSP, HTML) are not read.
- `mvc_url_prefix` = the most frequent prefix of all absolute `.mvc` URLs in JSPs and in JS files up to 512 KB (for
  example `/AB1/AB1C10`).
- REST calls in JS: `api.get/post/put/del(...)`, `fetch(...)`, `$.ajax({url, type})`. A dynamic path
  `'/api/x/'+id` is normalised to `/api/x/{id}`. In a JSP the script blocks and the event-handler attributes
  (`onclick="api.post(…)"`) are read too: a call that is not seen costs its endpoint (3.4, the exposure rule).

### 2.6 SQL file and datasource

```xml
<queryMap desc="..." xmlns="...">
  <query id="selectUserById" isNamed="false"><![CDATA[ SELECT USER_ID FROM TB_USER WHERE USER_ID = ? ]]></query>
</queryMap>
```

Parameters are positional `?`, or `:name` with `isNamed="true"`. The namespace URI is not checked (it carries a
company host name). *sbmig*: in a query with `isNamed="true"` only `:name` is a parameter and a `?` stays (it is
never numbered); in every other query both are replaced, as section 3.5 says. A named query that keeps a bare
`?` gets an error note: the database driver will read it as a parameter marker.

Datasource (next increment): a `<bean class="…DataSource">` with `url|jdbcUrl`, `driverClassName|driverClass|driver`,
`username|user`, `password`, any namespace; `${placeholder}` resolved from the project's `*.properties`, at most five
rounds; vendors `oracle`, `mysql`, `mariadb`, `postgresql`, `sqlserver`, `tibero`, `h2`. A JNDI lookup
(`jee:jndi-lookup`), no datasource or an unknown vendor changes nothing and gives `configured=false`.

## 3. Output

### 3.1 Layout

```
<root>/
├─ pom.xml                (parent, packaging pom)
├─ {p}-boot/     (war)   application class, config/, interceptor/, application.yml,
│                         resources/mybatis/mapper/, webapp/WEB-INF/static/{p}/
├─ {p}-domain/   (jar)   domain/{entity, logic, proxy, spec, store, util}     no Spring dependency
├─ {p}-service/  (jar)   logic/(*SpringLogic), rest/(*Controller, *PageController)
└─ {p}-store/    (jar)   store/(*MybatisStore)
```

Dependencies: `boot → service, store, domain`; `service → domain`; `store → domain`; `domain` → the Jakarta
annotation API only. Never in the result: a `javax.*` EE import, a class of the framework, a changed public signature
of code that was carried over. JSP / JS / CSS / images are copied, never given to an LLM.

### 3.2 Names

| what | rule |
|---|---|
| project name `{p}` | *sbmig*, the user's rule: the root pom's artifactId in camelCase (`shop-app` → `shopApp`). Only a project without a readable root pom falls back to the guide's rule: the name without extension; one prefix of `naming.name_prefixes` and one suffix of `naming.name_suffixes` removed; kebab → camelCase; `Project` appended |
| domain | not used when the names come from the pom. Fallback: one of `naming.domains`, else `naming.default_domain` |
| package `{pkg}` | the root pom's groupId + `.{p}`. Fallback: `{naming.package_root}.{domain}.{p}` (`convert.package_root`; empty = such a project is refused) |
| interfaces | `{X}Service` in `{pkg}.domain.spec`, `{X}Store` in `{pkg}.domain.store` |
| implementations | `{X}Logic` (plain class, `{pkg}.domain.logic`), `{X}SpringLogic` (`{pkg}.logic`, service module), `{X}MybatisStore` (`{pkg}.store`, store module) |
| controllers | `{X}Controller` (REST), `{X}PageController` (`.mvc`), both in `{pkg}.rest` |
| `{X}` | the service name without `-service`, split at `-` and `_`, each part's first letter upper-cased, the rest kept (`ab1234` → `Ab1234`, `itemLOV` → `ItemLOV`) |
| mapper namespace | `{pkg}.store.{X}Store` (not the interface's real name `{pkg}.domain.store.{X}Store`; calls go by string, so it works; open decision D12) |
| mapper file | `{kebab}-mapper.xml` |
| statement id | with a `.` in it only the last part is used; the original id stays as a comment |
| router URL | `/api/{service name without -service, as written}/{transition}` |

### 3.3 Methods of a resource

A resource is one `{X}`: a service, a mapper, or both under the same name.

- Every method has the signature `(Map<String,Object> params)`.
- From a service: with a router only the activities a router transition points at become methods; without one every
  activity that is not `router` / `user`. Method name: the mapper statement's id (last part, first letter lower case)
  when the `sql-key` resolves, else the `sql-key`'s last part, else the activity name. The same name twice is one
  method when it is the same statement; *sbmig*: another statement under a taken name gets `{name}2`, `{name}3`, …
  (the earlier tool let two transitions call one statement).
- *sbmig*: a `sql-key` `a.b` is resolved against every mapper (file stem or original id prefix `a`, statement `b`),
  and the store calls the statement in the mapper where it was found. The earlier tool looked only in the mapper
  named like the service and threw `UnsupportedOperationException` otherwise.
- From a mapper: every statement that has no method yet is appended. Returns: select → list, except a name with
  `count` / `total` / `size` / `exists` → int; insert / update / delete → int. Words are whole words of the name
  (`countAll`, `getTotal`; not `selectAccountList`). Several mappers with the same `{X}` (a converted SQL file and
  a copied mapper) all give their statements to the one resource, each method calling its own mapper's namespace.
- Two resources whose `{X}` differ only in case: the later one is renamed `{X}2`, … (class names only), so that
  the files do not overwrite each other on a case-insensitive disk. Every resource also gets a `rest_base`, the
  name after `/api/` in its controller's address: a resource with a router keeps its name as written and reserves
  it (compared exactly, as Spring compares paths); every other resource gets `kebab(resource)`, unique ignoring
  case among those and different from every reserved router name (a clash is numbered `-2`, …).
- `{X}Service` has every method, `{X}Store` only the `db_backed` ones, `{X}Logic` every method (a method that is not
  `db_backed` throws `UnsupportedOperationException` under a comment `TODO[<pass_hit>]` with the reason).

`{X}Logic` bodies by kind:

| kind | body |
|---|---|
| `upsert` | `params.get("rows")` as a list; `store.x(row)` per row, the sum is returned (no list: 0) |
| `delete` | `ids` a list: `store.x({id})` per item, summed; else `store.x(params)` |
| `pagesearch` | `page` defaults to 1 and `rows` to 10, then `store.x(params)` |
| `sortsearch`, other DB methods | `store.x(params)` |

### 3.4 Generated classes

- `{X}MybatisStore`: `@Repository`, `@Autowired SqlSession`; a method calls `"{namespace}.{id}"`; without a statement
  it throws `UnsupportedOperationException`. An `int` method on `selectOne` takes a number as it is and, because
  every converted select has `resultType="java.util.Map"`, the single value of a one-entry map.
- `{X}SpringLogic`: `@Service`; every method `@Transactional` (`readOnly = true` for a select); the body delegates to
  `new {X}Logic(store)`; `log.debug` on entry.
- `{X}Controller` from a mapper: `@RestController @RequestMapping("/api/{rest_base}")` (the resource in kebab); answer
  `{ok: true, data: …}`. *sbmig* (the guide does not say how parameters arrive): GET and DELETE take the request
  parameters; a POST endpoint has a form handler (`consumes` form / multipart) and a JSON handler (`consumes` JSON,
  optional body merged over the parameters) so that a form post, a JSON post and a POST without a body each reach
  exactly one of them; a `{name}` in the path is put into the parameters.
- `{X}Controller` of a service with a router replaces the mapper one: per transition
  `@RequestMapping(value = "/{t}", method = {GET, POST})` with `@RequestParam Map<String,Object> params`; answer
  `{ok: true, rows: result, [params.ResultName]: result}`; a transition name that is a Java keyword gets a `_` suffix
  as method name.
- A service with a router of which no method reaches a statement gets a stub `{X}SpringLogic` (every method throws
  `UnsupportedOperationException`), no `{X}MybatisStore`, and a hand-over item `{service}::SpringLogic`
  `[STUB_SPRING_LOGIC]`. The result must still compile.
- `{X}PageController`: `GET|POST {mvc_url_prefix}/{kebab}.mvc` returns the view name, the path under the static
  folder without `.jsp` (for example `springmvc/ab1c000010`); no model attributes. With no page routes from the front
  end the copied JSPs are scanned: folders `common, fragment, fragments, include, tiles, layout` and routes
  `error, dashboard, main, index, home` are left out; the same file name in two folders: the shortest path wins.
  *sbmig*: a page controller serves every URL the source writes for its route (`page_urls`) besides the kebab
  form, and in the scan mode the file's own spelling; a file whose extension is not exactly `.jsp` gets none.

Which statement becomes which REST endpoint (mapper controller):

| statement | endpoint |
|---|---|
| select, id starts with `lov` | none (the list-of-values controller, open decision D13) |
| select, id `getsummary` or ending `summary` | `GET /summary` |
| select, id one of `getlist find list selectall selectlist getlistpaged` | `GET ""` |
| select, other | `GET /{kebab(id)}` |
| insert / update, id one of `upsert save insert update merge` | `POST ""` |
| insert / update, other | `POST /{kebab(id)}` |
| delete, id `delete` | `DELETE /{id}` |
| delete, other | `DELETE /{kebab(id)}` |

Ids are compared in lower case. When the front end's REST calls (2.5) match at least one endpoint of this
resource, only the endpoints it calls are exposed; when none matches, all are. A call names a resource by its first
path part after `/api` (compared in kebab form); `{…}` matches any one path part; the query string is ignored.
An endpoint removed this way stays on its method as `unexposed` and `resources.unexposed_endpoints` lists them, so
that a 404 can be traced to the rule.
*sbmig*: two statements that land on the same verb and path: the first keeps it, the next gets `/{kebab(id)}`.

Which `SqlSession` call a method makes:

| returns | condition | call |
|---|---|---|
| list | — | `selectList` |
| int | the SQL is a select and the name has `count total size exists` | `selectOne` |
| int | the SQL verb is insert / update / delete | that verb |
| int | verb unknown, name has `delete remove` | `delete` |
| int | verb unknown, name has `insert save create add` | `insert` |
| int | verb unknown, name has `update modify upsert merge` | `update` |
| int | anything else | `update` |
| map | — | `selectOne` |

The verb is the first SQL keyword of the statement's body, not the XML tag (mappers with a wrong tag exist). `MERGE`
counts as insert.

### 3.5 SQL file → mapper

- Element by the first SQL keyword: `SELECT` / `WITH` → select, `INSERT`, `UPDATE`, `DELETE` → their own, anything
  else → select.
- `:name` → `#{name}` unless the character before it is `:` or a word character (`x::int` stays); every `?` left →
  `#{param1}` … `#{paramN}`.
- One parameter or more: `parameterType="map"`; a select: `resultType="java.util.Map"`.
- An insert whose column list has no `id`: `useGeneratedKeys="true" keyProperty="id"`.
- The body always sits in `<![CDATA[ … ]]>`. A file that cannot be parsed gives an empty mapper with a comment that
  says why, and an error entry.

### 3.6 Front-end copy

- The path after `WebContent` or `webapp`; a leading `WEB-INF` and a following `jsp/` are dropped
  (`WEB-INF/jsp/p/z.jsp` → `static/{p}/p/z.jsp`). Without either folder only the file name is kept (two files may
  collide; the second is an error entry, not an overwrite — *sbmig*).
- Target: `{p}-boot/src/main/webapp/WEB-INF/static/{p}/`. Bytes are copied unchanged; a file the ajax transformer
  rewrites keeps its encoding. A file reached through a symbolic link that leaves the source root is not copied.
- *sbmig*: after the copy, an absolute path in `<%@ include file>`, `<jsp:include page>`, `<jsp:forward page>` and
  `<c:import url>` that names a file the copy moved (`/WEB-INF/jsp/common/header.jsp`) is rewritten to where it went
  (`/WEB-INF/static/{p}/common/header.jsp`); a path whose file was not copied is a hand-over item `[JSP_PATH]`.
  Relative paths and dynamic values are left alone. The earlier tool left these pages broken.

### 3.7 Skeleton, datasource, gate, build

**What sbmig generates** (`skeleton.py`, `datasource.py`): a root pom (packaging pom, parent
`spring-boot-starter-parent` at the target Boot version, the four modules; the restructure replaces this parent by
the company parent afterwards), the module poms (domain: Jakarta annotation API only; store: domain, the MyBatis
starter, Spring context; service: domain, Spring Web MVC, Spring TX, slf4j; boot, a war: the three modules,
`spring-boot-starter-webmvc`, the JDBC starter, the MyBatis starter, embedded Jasper and JSTL without a second
servlet API jar, the vendor's JDBC driver when the datasource was found, `spring-boot-maven-plugin`), the application
class, one Web MVC configuration class (view prefix and suffix, one resource handler per top folder of the copied
static files, never serving a `.jsp` as text) and `application.yml` (`spring.servlet.encoding`, the MyBatis mapper
location, and the datasource URL / driver / user name when found). A version is written only for what the Boot
parent does not manage (the MyBatis starter: `convert.versions`, an empty value = the newest release the repository
has). The password is NEVER carried over: `application.yml` holds `${SPRING_DATASOURCE_PASSWORD:}` and a hand-over
item `[DATASOURCE_PASSWORD]` names the file it was in. The rule for everything else of the datasource is "write only
what cannot hold a secret": credentials inside the JDBC URL are stripped (`user/pw@`, `//user:pw@host`, password
parameters, for every vendor and wrapper prefix), then a wall judges a normalised copy of the URL (percent-decoded,
NFKC, case-folded) and withholds the whole URL when it still shows a password-like word, a token / key word, an
`@` other than the first one of an Oracle scheme, a `user` key with a value that is not a plain name, whitespace,
or a parameter name that was encoded (`datasource.may_carry_credentials`); a user name is written only when it is a
plain name, a driver class only when it looks like one. A withheld URL appears nowhere. No datasource found, or the
URL withheld = no datasource keys and `[DATASOURCE]`;
a vendor without a driver coordinate (`convert.jdbc_drivers`) = `[DATASOURCE_DRIVER]`. Not generated: the Gson
converter, OpenAPI, the method filter, the interceptors, the exception handler, any H2 demo configuration. The
guide's gate is not ported as such: the screen check is part of the pipeline (`FRONTEND_MISSING`), the build and
the LLM check are sbmig's own build gate, fix loop and structure review on the generated project.

**The earlier tool's rules, for reference:**

- `config/`: exception handler, method filter (allowed HTTP methods), Gson message converter, OpenAPI, persistence
  (`@EnableTransactionManagement`), Web MVC (view prefix `/WEB-INF/static/{p}/`, suffix `.jsp`; resource handlers
  `/{p}/**`, `/include/**`, `/springmvc/**`, `/css/**`, `/js/**`, `/images/**`). `interceptor/`: authentication
  (everyone passes as guest), request log. Application class: `@SpringBootApplication(scanBasePackages = …)`,
  `@EnableScheduling`, `@MapperScan("…store")`, extends `SpringBootServletInitializer`. `application.yml`: UTF-8
  forced, whitelabel page off, `mybatis.mapper-locations: classpath:mybatis/mapper/*.xml`,
  `map-underscore-to-camel-case: true`, springdoc on, DEBUG for the company package.
- Poms of the earlier tool: parent `spring-boot-starter-parent` 3.5.13, Java 17, version `1.0.0`; domain: Jakarta
  annotation API; store: domain, `spring-context`, `mybatis-spring-boot-starter` 3.0.5; service: domain,
  `spring-boot-starter-web`, `spring-boot-starter-aop`, `spring-tx`; boot (war, `finalName` `{p}`): service, store,
  starter-web, starter-jdbc, the MyBatis starter, `tomcat-embed-jasper`, `jakarta.servlet.jsp.jstl` 3.0.1, Gson
  2.10.1, `springdoc-openapi-starter-webmvc-ui` 2.8.6, H2 (runtime), `spring-boot-maven-plugin`. **sbmig writes no
  version from this list**: the target line, the parent chain and the repository decide (open decision T1).
- Datasource found (`configured=true`): the real `spring.datasource.*` and the vendor's JDBC driver, no H2. Not
  found: the earlier tool packaged an H2 demo configuration and rewrote SQL for H2 (open decision D3; the rewrite
  changes what queries mean and is not ported).
- Gate, three parts, all must pass: (1) an LLM check of the result (score ≥ 70 and no component judged `fail`; a model
  other than the converting one; input caps 6,000 characters per file, 30 files per part, 140,000 in all; order
  `store, mapper, logic, service, rest, controller, dao, domain, proxy, entity, util`; the skeleton is left out; the
  prompt carries an inventory of the result so that "not shown" is not read as "missing"), (2) the Maven build,
  (3) no source `.jsp` / `.js` / `.css` missing under `static/{p}/`. The gate is never relaxed.
- Build of the earlier tool: `mvn -q -DskipTests -f <parent pom> package`, 600 s. sbmig builds with its own runner.

### 3.8 Annotation mapping for carried-over classes (next increment)

Applies only to classes that are carried over (utility, entity, proxy), never to DAO / service / controller code:
`@{Fw}Service` / `@{Fw}Repository` / `@{Fw}Component` (with or without a name) → the Spring annotation;
`@{Fw}Controller("/p")` → `@Controller` + `@RequestMapping("/p")`; `@{Fw}Action(path="/x", method="GET"|"POST")` →
`@GetMapping("/x")` / `@PostMapping("/x")`; `@{Fw}Inject` → `@Autowired`; `@{Fw}Transaction[(readOnly = true)]` →
`@Transactional[(readOnly = true)]`; `@{Fw}Scheduled(cron="…")` → `@Scheduled(cron = "…")`; `extends {Fw}BaseDao` →
removed, a `JdbcTemplate` field injected, `queryForList(` → `jdbcTemplate.query(`, `queryForObject(` /
`queryForInt(` → `jdbcTemplate.queryForObject(`, `update(` → `jdbcTemplate.update(`; `{Fw}Model` / `{Fw}Request` /
`{Fw}Exception` → `Model` / `HttpServletRequest` (jakarta) / `RuntimeException` (imports only).
Defects of the earlier tool that must not be repeated: the imports of `@GetMapping` / `@PostMapping` /
`@RequestMapping` were not added; an action with method `PUT` / `DELETE` stayed unconverted with its import gone; the
name of `@{Fw}Inject("name")` was dropped without a `@Qualifier`; the DAO method rename left argument lists that do
not fit `JdbcTemplate`.

## 4. Hand-over items

One record per thing a person must look at: `component`, `reason`, `source_path`, `last_error`, `excerpt` (at most
2,000 characters of the source). A `reason` is written in the converter's own words (a line number, never the line):
reports show it. `last_error` and `excerpt` may hold project text; they are scrubbed and written only to the run's
`convert/queue.json`. A method that will fail when called is never silent: it has an item or a `no address:` line.

| reason | component | written by |
|---|---|---|
| `risk_score>=0.7` | the class | component classification |
| `[<tag>]`, `[2b]`, `[3]`, `[java_only]` + the evidence | `{service}::{activity}` | service decomposition |
| `[BEAN_DIALECT]` | the file | service decomposition |
| `[CUSTOM_ACTIVITY]` | the project's own activity class (one item per class) | service decomposition |
| `[AMBIGUOUS_SQL_KEY]`, `[UNKNOWN_SQL_KEY]`, `[NO_SQL_KEY]` | `{service}::{activity}`: the key is defined by two SQL files / by none / the DB activity names no key; the method has nothing to call and fails when called | service decomposition |
| `[STUB_SPRING_LOGIC]` | `{service}::SpringLogic` | controller generation |
| `[ENDPOINT_DUPLICATE]`, `[ENDPOINT_VERB]` | the resource's controller | controller generation |
| `[AJAX_UNMATCHED]` | `js_ajax::{file}` (JS and JSP files) | ajax transformer |
| `[AJAX_NO_CONTROLLER]` | `js_ajax::{file}` | the check of rewritten calls against the router controllers (`resources.ajax_targets_without_controller`): the service named was not converted, has no router, or is spelt in another case |
| `[JSP_PATH]` | `jsp_path::{file}` | front-end copy |
| `[DATASOURCE]`, `[DATASOURCE_PASSWORD]`, `[DATASOURCE_DRIVER]` | `datasource` | datasource step |
| `FRONTEND_MISSING` | `frontend::{file}` | the pipeline's screen check |

## 5. Not ported from the earlier tool

Starting / stopping the application, port checks, opening a browser; its demo paths; Windows-only process handling;
the H2 schema / data injection and the H2 SQL rewrite; the list-of-values controller's demo columns (the company's
code table decides, D13); its single-dispatcher controller generator; its demo UI and slide script; the in-place
mapper `resultType` correction at build time (generation and build stay apart).

## 6. Known limits carried over, to decide with the company

| id | what | today |
|---|---|---|
| T1 | target line of the generated project | *sbmig*: sbmig's target (JDK 21, Boot 4.1.0), versions never written from the guide |
| D2 | packaging | executable WAR, embedded Tomcat not `provided` |
| D3 | no datasource found | not decided; nothing of the H2 demo is ported |
| D4 | database password | plain text in `application.yml` in the earlier tool |
| D6 | activities with Java code only | stub + hand-over item (*sbmig*) |
| D7 | authentication | everyone is a guest |
| D8 | external calls | stub + hand-over item |
| D9 | batch classes | hand-over item |
| D10 | JSON answer shape | two shapes (`{ok,data}`, `{ok,rows,…}`), Gson |
| D11 | injection style | field `@Autowired` |
| D12 | mapper namespace | `{pkg}.store.{X}Store` |
| D13 | list-of-values controller | not generated |
| D14 | screen gate | compares file names only in the earlier tool; relative paths planned |
| D16 | error text and OpenAPI exposure | exposed in the earlier tool |
| — | resource handlers | six fixed paths; another top folder of the source is not served |
| — | positional parameters | `?` becomes `#{param1}` …; the caller must send keys of those names, the guide does not say who does |
| — | web files that are not copied | `.jspf`, tag files, static files inside `WEB-INF`: listed, a JSP that needs one gets `[JSP_PATH]` |
| — | context path | a context path written inside an ajax string literal is dropped with the rest of the literal's start |
| — | POST content types | a mapper controller's POST takes form, multipart, JSON or no body; another content type (`text/plain`) answers 415 |
| — | services without a router | get no address of their own; a rewritten call that names one is handed over (`[AJAX_NO_CONTROLLER]`) |
| — | mapper detection | an existing mapper outside a path that contains `mybatis` is not recognised |
| — | memory | the work budget bounds time, not memory: a hostile 4 MB script file (brackets or quotes only) peaks near 450 MiB while it is scanned |
| — | router controllers take `@RequestParam` only | an upsert's `rows` never arrives as a list from a form post |

## 7. Acceptance scenarios

| # | input | expected |
|---|---|---|
| A1 | SQL file with `WHERE A=? AND B=?` | `#{param1}`, `#{param2}`, `parameterType="map"`, body in CDATA |
| A2 | `:name` and `x::int` together | only `#{name}` replaced, `::int` kept |
| A3 | INSERT without an `id` column | `useGeneratedKeys="true" keyProperty="id"` |
| A4 | id `order-item.getList` | id `getList`, the original id as a comment |
| A5 | `<`, `>`, `&` in the SQL body | a mapper, no parse error |
| A6 | service XML: router + an upsert + a delete activity | spec has all, store the DB methods, Logic the upsert and delete loops |
| A7 | an external-call activity | not in the store, Logic stub, hand-over `[<tag>]` |
| A8 | a user activity whose source calls two mappers | `ambiguous`, hand-over `[3]` |
| A9 | JS with one call of each pattern A to F | all rewritten to `/api/...`; what cannot be resolved is `AJAX_UNMATCHED` |
| A10 | a router service without anything to call | stub SpringLogic, `[STUB_SPRING_LOGIC]`, compiles |
| A11 | one JSP missing in the result | screen part fails, gate fails, `FRONTEND_MISSING` |
| A12 | `WEB-INF/jsp/p/z.jsp` | `static/{p}/p/z.jsp` |
| A13 | file `sample-shop-clean.zip`, domain `d1` | project `shopProject`, package `{root}.d1.shopProject` |
| A14 | all of it with real Maven | the four-module reactor builds, `{p}-boot/target/{p}.war` exists |
| A15 | one gate part fails | gate fails, hand-over items and the check report exist |

A1 to A13 are unit tests of `tests/fwconvert/` (A13 holds for a project without a root pom; with one the names
come from the pom). A14: `tests/fwconvert/test_pipeline_maven.py` (integration) and the fixture
`fixtures/projects/fw-convert-basic`, a whole run with real Maven. A15 as the guide states it does not apply: there
is no three-part gate, a failed build is the run's own failure.

## 8. Modules (`sbmig/fwconvert/`)

| file | what | entry points |
|---|---|---|
| `model.py` | shared records | `ProjectIdentity`, `Layout`, `Statement`, `MapperFile`, `MapperIndex`, `Activity`, `DbVerdict`, `ServiceDef`, `Method`, `Endpoint`, `Resource`, `RestCall`, `FrontendApiMap`, `QueueItem`, `GeneratedFile` |
| `rules.py` | the vocabulary with the name filled in | `load_rules`, `ConvertRules` |
| `naming.py` | name rules of 3.2 | `kebab`, `camel`, `pascal`, `x_name`, `lower_first`, `java_safe`, `last_token` |
| `identity.py` | project name, domain, package | `derive_identity` |
| `inventory.py` | file kinds, component kind / risk / order | `scan_tree`, `classify_component`, `conversion_order`, `risk_queue` |
| `sqlmap.py` | SQL file → mapper, mapper copy | `convert_sql_text`, `read_mapper`, `convert_mappers` |
| `servicexml.py` | service XML, activity kinds, DB classifier | `parse_services`, `parse_service_xml`, `classify_activity`, `decompose_services` |
| `resources.py` | services + mappers → resources with methods and endpoints | `build_resources`, `ajax_targets_without_controller`, `unexposed_endpoints`, `unreachable_methods` |
| `javagen.py` | every generated Java class | `generate_resource`, `generate_page_controllers`, `duplicate_mappings`, `write_generated` |
| `frontcopy.py`, `apimap.py`, `ajax.py` | front-end copy and JSP path fix-up, page routes and REST calls, ajax rewrite | `copy_frontend`, `fix_jsp_paths`, `extract_api_map`, `scan_skipped`, `transform_ajax` |
| `datasource.py`, `skeleton.py` | the datasource of the source project, the poms and the three skeleton files | `read_datasource`, `scan_datasource`, `generate_skeleton` |
| `pipeline.py` | the steps in order, the border checks, the screen check; whether there is anything to convert | `convert_project`, `has_convertible`, `convertible_summary` |

Generators return `GeneratedFile` records (path under the output root + text) and write nothing themselves, except
the front-end copy, the JSP path fix-up and the ajax rewrite, which work on files. Project files are untrusted
input: XML is parsed without entity resolution, DTD loading or network access, every scan is linear or bounded
by a size cap, and no path may leave its root. The script scanner of `apimap.py` also has a work budget per file
(a small multiple of the file's length): a file that uses it up gives no calls and no page URLs and is named by
`apimap.scan_skipped`, whatever shape made it slow; so is a file whose scan raises. Only regular files are read:
`scan_tree` leaves out anything else (a named pipe would block a reader for ever) and every later step takes its
file lists from it.

## 9. Not built yet

Carried-over classes with the annotation mapping (3.8): the project's own Java classes (utilities, entities) are not
in the generated project, and both reports say so. The other way of handling a framework project (the framework
upgraded in place) waits for its document. `convert/queue.json` (every hand-over item with its source excerpt) is
in the run folder only, not in a download. Not run: a real framework project, the company cluster.
