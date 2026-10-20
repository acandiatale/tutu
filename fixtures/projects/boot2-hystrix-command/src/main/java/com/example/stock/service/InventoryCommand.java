package com.example.stock.service;

import com.netflix.hystrix.HystrixCommand;
import com.netflix.hystrix.HystrixCommandGroupKey;

/** Programmatic Hystrix command (no @HystrixCommand annotation): the remote call with a fallback of 0. */
public class InventoryCommand extends HystrixCommand<Integer> {

    private final InventoryGateway gateway;
    private final String sku;

    public InventoryCommand(InventoryGateway gateway, String sku) {
        super(HystrixCommandGroupKey.Factory.asKey("inventory"));
        this.gateway = gateway;
        this.sku = sku;
    }

    @Override
    protected Integer run() throws Exception {
        return gateway.available(sku);
    }

    @Override
    protected Integer getFallback() {
        return 0;
    }
}
