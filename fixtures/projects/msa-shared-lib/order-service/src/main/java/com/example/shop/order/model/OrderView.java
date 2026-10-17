package com.example.shop.order.model;

import java.util.List;

import javax.validation.constraints.Min;
import javax.validation.constraints.NotBlank;

import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;
import lombok.Value;

@Value
@Builder
public class OrderView {
    String orderId;
    String userName;
    List<Line> lines;

    @Data
    @NoArgsConstructor
    @AllArgsConstructor
    public static class Line {

        @NotBlank
        private String sku;

        @Min(1)
        private int quantity;
    }
}
