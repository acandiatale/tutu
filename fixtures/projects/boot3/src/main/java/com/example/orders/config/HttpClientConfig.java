package com.example.orders.config;

import org.springframework.boot.web.client.RestTemplateBuilder;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.client.RestTemplate;

@Configuration
public class HttpClientConfig {

    @Bean
    public RestTemplate paymentRestTemplate(RestTemplateBuilder builder, PaymentProperties props) {
        return builder
                .rootUri(props.baseUrl())
                .setConnectTimeout(props.connectTimeout())
                .setReadTimeout(props.readTimeout())
                .build();
    }
}
