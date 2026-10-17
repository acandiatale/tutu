package com.example.catalog.web;

import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.math.BigDecimal;
import java.util.Collections;

import org.junit.Before;
import org.junit.Test;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import com.example.catalog.model.Product;
import com.example.catalog.service.ProductService;

public class ProductControllerTest {

    private MockMvc mockMvc;
    private ProductService service;

    @Before
    public void setUp() {
        service = mock(ProductService.class);
        mockMvc = MockMvcBuilders.standaloneSetup(new ProductController(service))
                .addInterceptors(new AuditInterceptor())
                .build();
    }

    @Test
    public void apiReturnsJson() throws Exception {
        when(service.list()).thenReturn(Collections.singletonList(new Product(1L, "Keyboard", BigDecimal.TEN)));
        mockMvc.perform(get("/products/api"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].name").value("Keyboard"));
    }
}
