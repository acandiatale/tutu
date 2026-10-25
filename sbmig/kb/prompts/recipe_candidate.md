You are reviewing a build fix that was applied during an automated Java 21 / Spring Boot 4.1 migration.
Decide whether the fix is a **purely mechanical** change that a declarative OpenRewrite recipe can reproduce on
other projects.

Answer with the single word `NONE` unless the fix is exclusively one or more of:
- a type / class rename or move (fully qualified name change),
- a package rename,
- a method rename,
- a Maven dependency change (relocation, add, remove) or a version bump of a dependency or plugin,
- a Maven property value change or removal,
- a Spring / `.properties` / `.yaml` property key rename (or value change),
- the generic javax → jakarta namespace migration.

If the fix involves anything else (new logic, changed method bodies, changed signatures, new classes, rewritten
configuration DSLs, project-specific names), answer `NONE`.

Otherwise output ONLY a single declarative OpenRewrite recipe in YAML — no prose, no explanation:

```
type: specs.openrewrite.org/v1beta/recipe
name: <PascalCaseName>
displayName: <short title>
description: <one sentence: what is changed and why>
recipeList:
  - <recipe id>:
      <option>: <value>
```

Rules:
- `name` must be one PascalCase word (letters and digits only, no dots), e.g. `RenameFooToBar`.
- Use at most 12 entries in `recipeList`, and ONLY these recipes with exactly these options
  (required options first; the ones in brackets are optional):
  - `org.openrewrite.java.ChangeType`: oldFullyQualifiedTypeName, newFullyQualifiedTypeName [ignoreDefinition]
  - `org.openrewrite.java.ChangePackage`: oldPackageName, newPackageName [recursive]
  - `org.openrewrite.java.ChangeMethodName`: methodPattern, newMethodName [matchOverrides, ignoreDefinition]
  - `org.openrewrite.java.ChangeStaticFieldToMethod`: oldClassName, oldFieldName, newClassName, newMethodName
  - `org.openrewrite.java.ReplaceConstant`: owningType, constantName, literalValue
  - `org.openrewrite.java.RemoveAnnotation`: annotationPattern
  - `org.openrewrite.java.dependencies.ChangeDependency`: oldGroupId, oldArtifactId, newGroupId, newArtifactId [newVersion]
  - `org.openrewrite.java.dependencies.UpgradeDependencyVersion`: groupId, artifactId, newVersion [overrideManagedVersion]
  - `org.openrewrite.java.dependencies.AddDependency`: groupId, artifactId, version [scope, onlyIfUsing]
  - `org.openrewrite.java.dependencies.RemoveDependency`: groupId, artifactId [scope]
  - `org.openrewrite.maven.ChangePluginGroupIdAndArtifactId`: oldGroupId, oldArtifactId, newGroupId and/or newArtifactId
  - `org.openrewrite.maven.UpgradePluginVersion`: groupId, artifactId, newVersion
  - `org.openrewrite.maven.ChangePropertyValue`: key, newValue
  - `org.openrewrite.maven.RemoveProperty`: propertyName
  - `org.openrewrite.java.spring.ChangeSpringPropertyKey`: oldPropertyKey, newPropertyKey
  - `org.openrewrite.java.spring.ChangeSpringPropertyValue`: propertyKey, newValue
  - `org.openrewrite.properties.ChangePropertyKey`: oldPropertyKey, newPropertyKey
  - `org.openrewrite.yaml.ChangePropertyKey`: oldPropertyKey, newPropertyKey
  - `org.openrewrite.java.migrate.jakarta.JavaxMigrationToJakarta` (no options)
- Option values are plain scalars. Never use `$`, `${…}` placeholders, backticks, shell commands or scripts.
- Do not include project-specific class names unless the rename is the generic library/framework change.

## Build errors that the fix resolved

{{ERRORS}}

## Signatures

{{SIGNATURES}}

## Deterministic labels of the fix (computed, may be incomplete)

{{LABELS}}

## The applied fix

{{CHANGES}}
