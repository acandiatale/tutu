package com.example.shop.order;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import java.util.List;

import org.junit.jupiter.api.Test;

import com.example.shop.order.client.UserClient;
import com.example.shop.order.model.OrderLine;
import com.example.shop.order.model.OrderView;
import com.example.shop.order.service.OrderService;

class OrderServiceTest {

    @Test
    void placeUsesUserName() {
        UserClient client = mock(UserClient.class);
        when(client.userName(7L)).thenReturn("Lee");
        OrderView view = new OrderService(client).place(7L, List.of(new OrderLine("A-1", 2)));
        assertEquals("Lee", view.getUserName());
        assertEquals(1, view.getLines().size());
    }
}
