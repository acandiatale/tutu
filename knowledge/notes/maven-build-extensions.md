---
title: Build extensions that cannot be resolved (wagon deploy transports in <build><extensions>)
keywords:
  - "Unresolveable build extension|<extensions>|<extension>"
  - "org\\.apache\\.maven\\.wagon|wagon-(webdav|webdav-jackrabbit|ssh|ssh-external|ftp|scm|http-lightweight)"
  - "dav:|davs:|scp:|scpexe:|sftp:|ftp:"
cases: [pure_java, spring_legacy, spring_boot, servlet_tomcat, msa]
phases: [restructure]
---
# Build extensions Maven could not resolve

`Unresolveable build extension: Plugin <g:a:jar:v> or one of its dependencies could not be resolved: Failed to read
artifact descriptor …` is raised while Maven builds the project MODEL, before any goal runs — so `mvn clean package`
cannot even start. The entry sits in a pom's `<build><extensions><extension>`.

## Wagon providers are deploy transports — remove them
`org.apache.maven.wagon:wagon-webdav`, `wagon-webdav-jackrabbit`, `wagon-ssh`, `wagon-ssh-external`, `wagon-ftp`,
`wagon-scm` only teach `mvn deploy` a URL scheme (`dav:`, `davs:`, `scp:`, `sftp:`, `ftp:`) of an old
`<distributionManagement>`. Packaging never needs them, and the company parent provides the deploy target (the company
Nexus over http(s), which needs no extension in Maven 3.9).
- Remove the `<extension>` element (and the `<extensions>` element when it becomes empty) from every pom that declares it.
- A `<distributionManagement>` left in a module pom whose URLs use one of those schemes is dead too: remove the block
  (the parent chain supplies it) and name it in the summary so the owners know the old deploy target is gone.
- Never add a `<repository>` / `<pluginRepository>`, a version, a mirror or credentials for the extension, and never
  decline because the repository "lacks" it: the artifact is not needed. Versions such as `1.0-beta-2` are 2007-era
  and often exist in a repository only as a jar without its pom — which is exactly why the descriptor cannot be read.

## Other extensions
An extension that is not a transport (an `os-maven-plugin` detector for protobuf, a lifecycle or build-cache extension)
does take part in the build: treat it like a plugin that cannot be resolved — a release the repository has, from the
Dependency versions section / parent-chain facts; never remove it blind.
