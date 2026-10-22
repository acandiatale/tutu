package com.example.members.repository;

import java.util.Optional;

import org.springframework.data.repository.PagingAndSortingRepository;

import com.example.members.domain.Member;

/**
 * Spring Data 2.7: PagingAndSortingRepository extends CrudRepository, so save / findAll() / deleteById compile.
 * Spring Data 3+ dropped CrudRepository from that hierarchy; this directory is under openrewrite.exclusions
 * (sbmig.e2e.yml), so MigratePagingAndSortingRepository never sees it — the spring-data-crud-repository handler
 * must add ListCrudRepository&lt;Member, Long&gt; (E14).
 */
public interface MemberRepository extends PagingAndSortingRepository<Member, Long> {

    Optional<Member> findByEmail(String email);

    boolean existsByEmail(String email);
}
