package com.example.catalog.web;

import java.util.List;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseBody;
import org.springframework.web.bind.annotation.ResponseStatus;

import com.example.catalog.model.Product;
import com.example.catalog.service.ProductService;

@Controller
@RequestMapping("/products")
public class ProductController {

    private final ProductService productService;

    @Autowired
    public ProductController(ProductService productService) {
        this.productService = productService;
    }

    @GetMapping
    public String list(Model model) {
        model.addAttribute("products", productService.list());
        return "products";
    }

    @GetMapping(value = "/api", produces = "application/json")
    @ResponseBody
    public List<Product> api() {
        return productService.list();
    }

    @GetMapping(value = "/api/{id}", produces = "application/json")
    @ResponseBody
    public Product one(@PathVariable("id") long id) {
        return productService.get(id);
    }

    @ExceptionHandler(IllegalArgumentException.class)
    @ResponseStatus(HttpStatus.NOT_FOUND)
    @ResponseBody
    public String notFound(IllegalArgumentException e) {
        return e.getMessage();
    }
}
