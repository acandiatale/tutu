package com.example.orders.aop;

import org.aspectj.lang.ProceedingJoinPoint;
import org.aspectj.lang.annotation.Around;
import org.aspectj.lang.annotation.Aspect;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import com.google.common.base.Stopwatch;

@Aspect
@Component
public class TimingAspect {

    private static final Logger LOG = LoggerFactory.getLogger(TimingAspect.class);

    @Around("within(com.example.orders.service..*)")
    public Object time(ProceedingJoinPoint pjp) throws Throwable {
        Stopwatch sw = Stopwatch.createStarted();
        try {
            return pjp.proceed();
        } finally {
            LOG.debug("{} took {}", pjp.getSignature().toShortString(), sw);
        }
    }
}
