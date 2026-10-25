package com.example.ops.batch;

import org.springframework.batch.core.Job;
import org.springframework.batch.core.Step;
import org.springframework.batch.core.configuration.annotation.EnableBatchProcessing;
import org.springframework.batch.core.configuration.annotation.JobBuilderFactory;
import org.springframework.batch.core.configuration.annotation.StepBuilderFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * Spring Batch 4 configuration: JobBuilderFactory / StepBuilderFactory are gone in Batch 5 (SpringBatch5To6Migration
 * chains SpringBatch4To5Migration).
 */
@Configuration
@EnableBatchProcessing
public class BatchConfig {

    @Autowired
    private JobBuilderFactory jobBuilderFactory;

    @Autowired
    private StepBuilderFactory stepBuilderFactory;

    @Bean
    public Job cleanupJob(Step cleanupStep) {
        return jobBuilderFactory.get("cleanupJob").start(cleanupStep).build();
    }

    @Bean
    public Step cleanupStep(CleanupTasklet cleanupTasklet) {
        return stepBuilderFactory.get("cleanupStep").tasklet(cleanupTasklet).build();
    }
}
