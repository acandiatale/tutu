package com.example.members.repository;

import java.util.Optional;

import org.springframework.data.jpa.repository.JpaRepository;

import com.example.members.domain.Member;

public interface MemberRepository extends JpaRepository<Member, Long> {

    Optional<Member> findByEmail(String email);

    boolean existsByEmail(String email);
}
