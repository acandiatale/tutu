package com.example.shop;

import static org.assertj.core.api.Assertions.assertThat;

import com.example.shop.core.GreetingService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest
class ShopApplicationTests {

    @Autowired
    private GreetingService greetingService;

    @Test
    void greets() {
        // both core and web ship a messages.properties: which one wins depends on the classpath order (as-is)
        // or on the merge-conflict policy (after the comp-jar merge: core, the first module in <modules>)
        assertThat(greetingService.greet("kim")).startsWith("hello from ").endsWith(", kim");
    }
}
