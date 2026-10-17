package com.example.catalog.service;

import java.util.List;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import com.example.catalog.model.Product;
import com.example.catalog.repository.ProductRepository;

@Service
public class ProductService {

    private final ProductRepository repository;

    @Autowired
    public ProductService(ProductRepository repository) {
        this.repository = repository;
    }

    public List<Product> list() {
        return repository.findAll();
    }

    public Product get(long id) {
        Product p = repository.findById(id);
        if (p == null) {
            throw new IllegalArgumentException("no product " + id);
        }
        return p;
    }
}
