---
title: Spring Batch 4/5 -> 6 (Boot 4.1 manages Batch 6.0)
keywords:
  - "org\\.springframework\\.batch"
  - "JobBuilderFactory|StepBuilderFactory|\\bJobBuilder\\b|\\bStepBuilder\\b"
  - "\\b(ItemReader|ItemWriter|ItemProcessor|ItemStream|Tasklet|Chunk|StepContribution|ChunkContext|RepeatStatus)\\b"
  - "EnableBatchProcessing|DefaultBatchConfigurer|BatchConfigurer|DefaultBatchConfiguration|EnableJdbcJobRepository"
  - "SimpleJobLauncher|TaskExecutorJobLauncher|JobExplorer|MapJobRepositoryFactoryBean|JobParametersInvalidException|JobExecutionListenerSupport"
  - "spring-batch-(core|infrastructure)|spring-boot-starter-batch"
cases: []
phases: [spring]
---
# Spring Batch 4 / 5 → 6 (Boot 4.1 manages Batch 6.0.x)

The `SpringBatch5To6Migration` recipe (when the plan notes list a Batch recipe set; Boot projects run it inside the
Boot umbrella) does most of this; what reaches you is what it left — often Batch 4 code that also needs the 5.0
changes. The artifacts stay Boot-managed (`org.springframework.batch:spring-batch-core`, `version: null`).

## Package moves (6.0.x jars; the class name stays, change the import)
- `org.springframework.batch.item.*` → `org.springframework.batch.infrastructure.item.*` (`ItemReader`, `ItemWriter`,
  `ItemProcessor`, `ItemStream`, `Chunk`, `ExecutionContext`, `file.*`, `database.*`, `support.*`)
- `org.springframework.batch.repeat.*` → `org.springframework.batch.infrastructure.repeat.*` (`RepeatStatus`)
- `batch.core.Job`, `JobExecution`, `JobInstance` → `org.springframework.batch.core.job`; `core.JobParameters`,
  `JobParametersBuilder`, `JobParametersIncrementer`, `core.launch.support.RunIdIncrementer` →
  `org.springframework.batch.core.job.parameters`; `core.JobParametersInvalidException` →
  `core.job.parameters.InvalidJobParametersException`
- `batch.core.Step`, `StepExecution`, `StepContribution` → `org.springframework.batch.core.step`
- `batch.core.JobExecutionListener`, `StepExecutionListener`, `ChunkListener`, `ItemReadListener`, `ItemWriteListener`,
  `SkipListener` → `org.springframework.batch.core.listener`; `JobExecutionListenerSupport` /
  `StepExecutionListenerSupport` are gone → implement the interface (default methods)
- `core.explore.JobExplorer` → `core.repository.explore.JobExplorer`; `core.repository.JobExecutionAlreadyRunningException`,
  `JobRestartException`, `JobInstanceAlreadyCompleteException` → `org.springframework.batch.core.launch`
- Unchanged: `core.step.tasklet.Tasklet`, `core.scope.context.ChunkContext`, `core.repository.JobRepository`,
  `core.launch.JobLauncher` / `JobOperator`, `core.BatchStatus`, `core.configuration.annotation.{EnableBatchProcessing,
  StepScope, JobScope}`.

## API changes (5.0 / 6.0)
- `JobBuilderFactory` / `StepBuilderFactory` are gone → inject `JobRepository` (and the `PlatformTransactionManager`)
  and build with `new JobBuilder("name", jobRepository)` / `new StepBuilder("name", jobRepository)`; keep job / step
  names and bean names.
- Chunk steps: `.<I, O>chunk(n).transactionManager(tm)` (6.0; `.chunk(n, tm)` still compiles, deprecated); tasklet
  steps: `.tasklet(t, tm)`.
- `ItemWriter.write(List<? extends T>)` → `write(Chunk<? extends T> chunk)` (`chunk.getItems()` for the list;
  symptom: "is not abstract and does not override abstract method write(Chunk)").
- `DefaultBatchConfigurer` / `BatchConfigurer` are gone → delete the subclass and declare what it supplied
  (`DataSource`, `PlatformTransactionManager`) as plain beans, or extend `DefaultBatchConfiguration`.
  `MapJobRepositoryFactoryBean` is gone (no in-memory map repository).
- `SimpleJobLauncher` → `TaskExecutorJobLauncher` (`JobLauncher` is deprecated for `JobOperator`; calls that compile stay).
- `JobExecution` / `StepExecution` `getStartTime()` / `getEndTime()` return `LocalDateTime`, not `Date`.

## fix-loop: spring-batch
- The parent chain / Boot BOM moved Batch to 6.0, not you: port the code javac names (imports, builders,
  `write(Chunk)`, removed configurer / listener-support classes) and leave the version. Name the jobs whose builders
  or writers you changed in `<verify>`.
- Never re-add `spring-batch-core` 4.x / 5.x with an explicit version, never add `spring-boot-starter-batch` to a
  converted project's boot module only to compile a class of another module.
- Runtime, not a build fix (say it in `<analysis>`; Handover): `@EnableBatchProcessing` is resourceless in 6.0 (JDBC
  metadata needs `@EnableJdbcJobRepository` or `spring-boot-starter-batch-jdbc`) and switches Boot's Batch
  auto-configuration off; the metadata schema changed (`BATCH_JOB_SEQ` …); failed 4 / 5 executions cannot be restarted.
