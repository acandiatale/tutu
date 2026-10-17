package com.example.shop.order.model;

import java.util.List;

import lombok.Builder;
import lombok.Value;

@Value
@Builder
public class OrderView {
    String orderId;
    String userName;
    List<OrderLine> lines;
}
