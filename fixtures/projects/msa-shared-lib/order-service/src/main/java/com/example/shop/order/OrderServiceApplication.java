package com.example.shop.order;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.web.client.RestTemplateBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.web.client.RestTemplate;

import com.example.shop.common.web.TraceIdFilter;

@SpringBootApplication
public class OrderServiceApplication {

    public static void main(String[] args) {
        SpringApplication.run(OrderServiceApplication.class, args);
    }

    @Bean
    public RestTemplate userServiceRestTemplate(RestTemplateBuilder builder) {
        return builder.rootUri("http://user-service:8080").build();
    }

    /** javax.servlet Filter from the shared library, registered by Boot as a plain Filter bean. */
    @Bean
    public TraceIdFilter traceIdFilter() {
        return new TraceIdFilter();
    }
}
