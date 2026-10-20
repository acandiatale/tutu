package com.example.stock.service;

import org.springframework.stereotype.Service;

@Service
public class StockService {

    private final InventoryGateway gateway;

    public StockService(InventoryGateway gateway) {
        this.gateway = gateway;
    }

    public int stockFor(String sku) {
        return new InventoryCommand(gateway, sku).execute();
    }
}
