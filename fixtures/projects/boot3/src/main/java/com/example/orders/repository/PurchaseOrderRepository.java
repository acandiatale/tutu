package com.example.orders.repository;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;

import com.example.orders.domain.PurchaseOrder;

public interface PurchaseOrderRepository extends JpaRepository<PurchaseOrder, Long> {

    List<PurchaseOrder> findByCustomerOrderByCreatedAtDesc(String customer);
}
