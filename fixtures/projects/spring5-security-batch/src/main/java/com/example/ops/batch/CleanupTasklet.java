package com.example.ops.batch;

import java.util.Arrays;

import org.springframework.batch.core.StepContribution;
import org.springframework.batch.core.scope.context.ChunkContext;
import org.springframework.batch.core.step.tasklet.Tasklet;
import org.springframework.batch.item.support.ListItemReader;
import org.springframework.batch.repeat.RepeatStatus;
import org.springframework.stereotype.Component;

/**
 * Removes expired export files. Batch 6 moved org.springframework.batch.item / .repeat to
 * org.springframework.batch.infrastructure.* (SpringBatch5To6Migration).
 */
@Component
public class CleanupTasklet implements Tasklet {

    @Override
    public RepeatStatus execute(StepContribution contribution, ChunkContext chunkContext) throws Exception {
        ListItemReader<String> expired = new ListItemReader<>(Arrays.asList("export-1.csv", "export-2.csv"));
        String name;
        while ((name = expired.read()) != null) {
            contribution.incrementWriteCount(name.isEmpty() ? 0 : 1);
        }
        return RepeatStatus.FINISHED;
    }
}
