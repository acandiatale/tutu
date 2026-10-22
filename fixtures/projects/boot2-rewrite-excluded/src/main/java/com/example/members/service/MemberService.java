package com.example.members.service;

import java.util.ArrayList;
import java.util.List;

import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;

import org.apache.commons.lang3.StringUtils;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import com.example.members.domain.Member;
import com.example.members.repository.MemberRepository;
import com.example.members.vendor.LegacyQueryHelper;
import com.example.members.web.MemberRequest;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;

@Slf4j
@Service
@RequiredArgsConstructor
public class MemberService {

    private final MemberRepository repository;

    @PersistenceContext
    private EntityManager em;

    @Transactional
    public Member join(MemberRequest request) {
        String email = StringUtils.lowerCase(StringUtils.trim(request.getEmail()));
        if (repository.existsByEmail(email)) {
            throw new DuplicateMemberException(email);
        }
        Member saved = repository.save(Member.builder().email(email).name(StringUtils.capitalize(request.getName())).build());
        log.info("member joined id={} email={}", saved.getId(), saved.getEmail());
        return saved;
    }

    @Transactional(readOnly = true)
    public List<Member> list() {
        List<Member> members = new ArrayList<>();
        repository.findAll().forEach(members::add);    // Iterable with CrudRepository, List with ListCrudRepository
        return members;
    }

    @Transactional(readOnly = true)
    public long countMembers() {
        return LegacyQueryHelper.count(em, "Member");
    }

    @Transactional
    public void remove(long id) {
        repository.deleteById(id);
    }

    public static class DuplicateMemberException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public DuplicateMemberException(String email) {
            super("already registered: " + email);
        }
    }
}
