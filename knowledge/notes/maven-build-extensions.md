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

A `<build><plugins><plugin>` declared with `<extensions>true</extensions>` is loaded into the same realm and fails with the
same text: a Wagon provider declared that way is removed the same way; a real plugin declared with `<extensions>true</extensions>`
(os-maven-plugin …) is a plugin-resolution problem — a version the repository has.

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

## Where the declaration is
The error block names the project that failed to LOAD (`The project g:a:v (…/module/pom.xml) has 1 error`), not
necessarily the pom that declares the extension: `<build><extensions>` is inherited, so a root / parent pom's entry fails
every module. Search every pom of the tree (profiles' `<build>` too) for the `<extension>` with that artifactId and
remove each occurrence; editing only the named module pom changes nothing when the declaration sits in its parent.
Note: the intake already removes the Wagon providers listed in `knowledge/libraries/build_extensions.yml` before the
first Maven call — an extension that still reaches this error is one that list does not know (add it there when it is
a transport) or one that is not a transport.

## Other extensions
An extension that is not a transport (an `os-maven-plugin` detector for protobuf, a lifecycle or build-cache extension)
does take part in the build: treat it like a plugin that cannot be resolved — a release the repository has, from the
Dependency versions section / parent-chain facts; never remove it blind.
