package com.example.shop.user.repository;

import org.springframework.data.jpa.repository.JpaRepository;

import com.example.shop.user.domain.AppUser;

public interface AppUserRepository extends JpaRepository<AppUser, Long> {
}
