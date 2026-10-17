package com.example.orders.web;

import static org.mockito.BDDMockito.given;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.math.BigDecimal;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.HttpStatus;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.server.ResponseStatusException;

import com.example.orders.domain.PurchaseOrder;
import com.example.orders.service.OrderService;

@WebMvcTest(OrderController.class)
class OrderControllerTest {

    @Autowired
    private MockMvc mockMvc;

    @MockBean
    private OrderService orderService;

    @Test
    void getOrder() throws Exception {
        given(orderService.get(1L)).willReturn(new PurchaseOrder("kim", new BigDecimal("10.5")));
        mockMvc.perform(get("/api/orders/1"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.customer").value("kim"))
                .andExpect(jsonPath("$.amount").value("10.50"));
    }

    @Test
    void missingOrderIs404() throws Exception {
        given(orderService.get(9L)).willThrow(new ResponseStatusException(HttpStatus.NOT_FOUND, "no order 9"));
        mockMvc.perform(get("/api/orders/9")).andExpect(status().isNotFound());
    }
}
