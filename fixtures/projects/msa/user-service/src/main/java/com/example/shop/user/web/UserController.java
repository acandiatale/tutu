package com.example.shop.user.web;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import com.example.shop.user.domain.AppUser;
import com.example.shop.user.repository.AppUserRepository;

@RestController
public class UserController {

    private final AppUserRepository repository;

    @Value("${user.greeting}")
    private String greeting;

    public UserController(AppUserRepository repository) {
        this.repository = repository;
    }

    @GetMapping("/api/users/{id}")
    public AppUser get(@PathVariable long id) {
        return repository.findById(id)
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND, "no user " + id));
    }

    @GetMapping("/api/users/greeting")
    public String greeting(@RequestParam String name) {
        return greeting + ", " + name;
    }

    @PostMapping("/api/users")
    public AppUser create(@RequestParam String name) {
        return repository.save(new AppUser(name));
    }
}
