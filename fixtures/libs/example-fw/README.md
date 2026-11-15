# example-fw — stand-in for the in-house framework

Placeholder coordinates only (the real framework's name never appears in this repository). Installed into the local
repository by `fixtures/libs/install.sh` (JDK 21, `--release 8`, so a JDK 8 as-is build can use them).

| artifact | packaging | what it is for |
|---|---|---|
| `com.example.fw:example-fw-core:1.0.0` | jar | the framework core: `FwStrings`, `FwResult`; plain Java, no javax / jakarta in its API |
| `com.example.fw:example-fw-web:1.0.0` | jar | depends on `example-fw-core` (the transitive scenario): `FwPaging` |
| `com.example.fw:example-fw-parent:1.0.0` | pom | declares `example-fw-core` in `<dependencies>` (children inherit it), manages `example-fw-web` and the compiler / war plugin versions |

Fixtures: `fw-direct` (declares the core), `fw-parent` (inherits it from the parent), `fw-transitive` /
`fw-transitive-note` (get it through `example-fw-web`).
