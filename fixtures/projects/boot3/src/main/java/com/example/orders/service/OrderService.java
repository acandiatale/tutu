package com.example.orders.service;

import java.math.BigDecimal;
import java.util.List;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import com.example.orders.client.PaymentClient;
import com.example.orders.domain.PurchaseOrder;
import com.example.orders.repository.PurchaseOrderRepository;
import com.google.common.base.Preconditions;

@Service
public class OrderService {

    private final PurchaseOrderRepository repository;
    private final PaymentClient paymentClient;

    public OrderService(PurchaseOrderRepository repository, PaymentClient paymentClient) {
        this.repository = repository;
        this.paymentClient = paymentClient;
    }

    @Transactional
    public PurchaseOrder place(String customer, BigDecimal amount) {
        Preconditions.checkArgument(amount.signum() > 0, "amount must be positive");
        return repository.save(new PurchaseOrder(customer, amount));
    }

    @Transactional
    public PurchaseOrder pay(long id) {
        PurchaseOrder order = get(id);
        if (order.getStatus() != PurchaseOrder.Status.NEW) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "order " + id + " is " + order.getStatus());
        }
        paymentClient.charge(id, order.getAmount());
        order.setStatus(PurchaseOrder.Status.PAID);
        return order;
    }

    @Transactional(readOnly = true)
    public PurchaseOrder get(long id) {
        return repository.findById(id)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "no order " + id));
    }

    @Transactional(readOnly = true)
    public List<PurchaseOrder> byCustomer(String customer) {
        return repository.findByCustomerOrderByCreatedAtDesc(customer);
    }
}
