package com.example.members.query;

import java.util.List;

import javax.persistence.EntityManager;
import javax.persistence.PersistenceContext;
import javax.persistence.Query;

import org.qlrm.mapper.JpaResultMapper;
import org.springframework.stereotype.Repository;

/**
 * QLRM 1.7.1 (ch.simas.qlrm, 2017) maps native-query rows to constructors — and takes the javax JPA Query type.
 * After the javax → jakarta migration this call only compiles with QLRM 4.0.1+ (org.qlrm, Jakarta Persistence):
 * handler jakarta-library-floor relocates / raises it (knowledge/libraries/jakarta_libraries.yml).
 */
@Repository
public class MemberSummaryQuery {

    @PersistenceContext
    private EntityManager em;

    public List<MemberSummary> summaries() {
        Query q = em.createNativeQuery("select id, email from member order by id");
        return new JpaResultMapper().list(q, MemberSummary.class);
    }
}
