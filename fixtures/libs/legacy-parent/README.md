# legacy-parent — simulated OLD in-house parent pom

`com.company.legacy:legacy-parent:1.0.0` (packaging `pom`, not on Maven Central; `fixtures/libs/install.sh` installs it)
stands in for the company parent a project inherited BEFORE the migration. Its `<dependencyManagement>` pins
`org.apache.commons:commons-text:1.10.0` and `org.apache.commons:commons-csv:1.10.0`, two libraries the Spring Boot 4.1 BOM
does not manage. A project that declares them without a version builds as-is; once the restructure moves its pom to the
role parents those versions are gone — the v2.1l carry-over (`fixtures/projects/legacy-external-parent`) pins them from the
as-is effective pom.
